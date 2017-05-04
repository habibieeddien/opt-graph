# Convert a trace created by the Valgrind OPT C backend to a format that
# the OPT frontend can digest

# Created 2015-10-04 by Philip Guo

# pass in full path name of a source file, which should end in '.c' or '.cpp'.
# assumes that the Valgrind-produced trace is $basename.vgtrace
# (without the '.c.' or '.cpp' extension)


# this is pretty brittle and dependent on the user's gcc version and
# such because it generates code to conform to certain calling
# conventions, frame pointer settings, etc., eeek
#
# we're assuming that the user has compiled with:
# gcc -ggdb -O0 -fno-omit-frame-pointer
#
# on a platform like:
'''
$ gcc -v
Using built-in specs.
COLLECT_GCC=gcc
COLLECT_LTO_WRAPPER=/usr/lib/gcc/x86_64-linux-gnu/4.8/lto-wrapper
Target: x86_64-linux-gnu
Configured with: ../src/configure -v --with-pkgversion='Ubuntu 4.8.4-2ubuntu1~14.04' --with-bugurl=file:///usr/share/doc/gcc-4.8/README.Bugs --enable-languages=c,c++,java,go,d,fortran,objc,obj-c++ --prefix=/usr --program-suffix=-4.8 --enable-shared --enable-linker-build-id --libexecdir=/usr/lib --without-included-gettext --enable-threads=posix --with-gxx-include-dir=/usr/include/c++/4.8 --libdir=/usr/lib --enable-nls --with-sysroot=/ --enable-clocale=gnu --enable-libstdcxx-debug --enable-libstdcxx-time=yes --enable-gnu-unique-object --disable-libmudflap --enable-plugin --with-system-zlib --disable-browser-plugin --enable-java-awt=gtk --enable-gtk-cairo --with-java-home=/usr/lib/jvm/java-1.5.0-gcj-4.8-amd64/jre --enable-java-home --with-jvm-root-dir=/usr/lib/jvm/java-1.5.0-gcj-4.8-amd64 --with-jvm-jar-dir=/usr/lib/jvm-exports/java-1.5.0-gcj-4.8-amd64 --with-arch-directory=amd64 --with-ecj-jar=/usr/share/java/eclipse-ecj.jar --enable-objc-gc --enable-multiarch --disable-werror --with-arch-32=i686 --with-abi=m64 --with-multilib-list=m32,m64,mx32 --with-tune=generic --enable-checking=release --build=x86_64-linux-gnu --host=x86_64-linux-gnu --target=x86_64-linux-gnu
Thread model: posix
gcc version 4.8.4 (Ubuntu 4.8.4-2ubuntu1~14.04)
'''


import json
import os
import pprint
import sys
from optparse import OptionParser

pp = pprint.PrettyPrinter(indent=2)

RECORD_SEP = '=== pg_trace_inst ==='


MAX_STEPS = 1000
ONLY_ONE_REC_PER_LINE = True

all_execution_points = []

# False if record isn't parsed properly or is an exception


def process_record(lines):
    if not lines:
        return True  # 'nil success case to keep the parser going

    err_lines = []
    stdout_lines = []
    regular_lines = []
    for e in lines:
        if e.startswith('ERROR: '):
            err_lines.append(e)
        elif e.startswith('STDOUT: '):
            stdout_lines.append(e)
        elif e.startswith('MAX_STEPS_EXCEEDED'):
            pass  # oof
        else:
            regular_lines.append(e)

    rec = '\n'.join(regular_lines)
    try:
        obj = json.loads(rec)
    except ValueError:
        print >> sys.stderr, "Ugh, bad record!"
        return False

    assert len(stdout_lines) == 1  # always have one!
    # it's encoded as JSON in a single line
    stdout_str = json.loads(stdout_lines[0][len('STDOUT: '):])

    # take the first error only
    err_str = err_lines[0] if err_lines else None

    x = process_json_obj(obj, err_str, stdout_str)
    all_execution_points.append(x)
    # it's a good idea to fail-fast on first exception since it's
    # pedagogically bad to keep executing despite errors
    if x['event'] == 'exception':
        return False
    return True


def process_json_obj(obj, err_str, stdout_str):
    # print '---'
    # pp.pprint(obj)
    # print

    assert len(obj['stack']) > 0  # C programs always have a main at least!
    obj['stack'].reverse()  # make the stack grow down to follow convention
    top_stack_entry = obj['stack'][-1]

    # create an execution point object
    ret = {}

    heap = {}
    stack = []
    enc_globals = {}
    ret['heap'] = heap
    ret['stack_to_render'] = stack
    ret['globals'] = enc_globals

    # sometimes there are no globals in a trace
    if 'ordered_globals' in obj:
        ret['ordered_globals'] = obj['ordered_globals']
    else:
        ret['ordered_globals'] = []

    ret['line'] = obj['line']
    # use the 'topmost' entry's name
    ret['func_name'] = top_stack_entry['func_name']

    if err_str:
        ret['event'] = 'exception'
        ret['exception_msg'] = err_str + \
            '\n(Stopped running after the first error. Please fix your code.)'
    else:
        ret['event'] = 'step_line'

    ret['stdout'] = stdout_str

    if 'globals' in obj:
        for g_var, g_val in obj['globals'].iteritems():
            enc_globals[g_var] = encode_value(g_val, heap)

    for e in obj['stack']:
        stack_obj = {}
        stack.append(stack_obj)

        stack_obj['func_name'] = e['func_name']
        stack_obj['ordered_varnames'] = e['ordered_varnames']
        stack_obj['is_highlighted'] = e is top_stack_entry

        # hacky: does FP (the frame pointer) serve as a unique enough frame ID?
        # sometimes it's set to 0 :/
        stack_obj['frame_id'] = e['FP']

        stack_obj['unique_hash'] = stack_obj[
            'func_name'] + '_' + stack_obj['frame_id']

        # unsupported
        stack_obj['is_parent'] = False
        stack_obj['is_zombie'] = False
        stack_obj['parent_frame_id_list'] = []

        enc_locals = {}
        stack_obj['encoded_locals'] = enc_locals

        for local_var, local_val in e['locals'].iteritems():
            enc_locals[local_var] = encode_value(local_val, heap)

    # pp.pprint(ret)
    # print [(e['func_name'], e['frame_id']) for e in ret['stack_to_render']]

    return ret


# returns an encoded value in OPT format and possibly mutates the heap
def encode_value(obj, heap):
    if obj['kind'] == 'base':
        return ['C_DATA', obj['addr'], obj['type'], obj['val']]

    elif obj['kind'] == 'pointer':
        if 'deref_val' in obj:
            encode_value(obj['deref_val'], heap)  # update the heap
        return ['C_DATA', obj['addr'], 'pointer', obj['val']]

    elif obj['kind'] == 'struct':
        ret = ['C_STRUCT', obj['addr'], obj['type']]

        # sort struct members by address so that they look ORDERED
        members = obj['val'].items()
        members.sort(key=lambda e: e[1]['addr'])
        for k, v in members:
            # TODO: is an infinite loop possible here?
            entry = [k, encode_value(v, heap)]
            ret.append(entry)
        return ret

    elif obj['kind'] == 'array':
        ret = ['C_ARRAY', obj['addr']]
        for e in obj['val']:
            # TODO: is an infinite loop possible here?
            ret.append(encode_value(e, heap))
        return ret

    elif obj['kind'] == 'typedef':
        # pass on the typedef type name into obj['val'], then recurse
        obj['val']['type'] = obj['type']
        return encode_value(obj['val'], heap)

    elif obj['kind'] == 'heap_block':
        assert obj['addr'] not in heap
        new_elt = ['C_ARRAY', obj['addr']]
        for e in obj['val']:
            # TODO: is an infinite loop possible here?
            new_elt.append(encode_value(e, heap))
        heap[obj['addr']] = new_elt
        # TODO: what about heap-to-heap pointers?

    else:
        assert False


if __name__ == '__main__':
    parser = OptionParser(usage="Create an OPT trace from a Valgrind trace")
    parser.add_option("--create_jsvar", dest="js_varname", default=None,
                      help="Create a JavaScript variable out of the trace")
    parser.add_option("--jsondump", dest="jsondump", action="store_true", default=False,
                      help="Dump compact JSON as output")
    parser.add_option("--prettydump", dest="prettydump", action="store_true", default=False,
                      help="Dump pretty-printed JSON as output")

    (options, args) = parser.parse_args()

    fn = args[0]
    basename, ext = os.path.splitext(fn)
    assert ext in ('.c', '.cpp')
    cur_record_lines = []

    success = True

    for line in open('usercode.vgtrace'):
        line = line.strip()
        if line == RECORD_SEP:
            success = process_record(cur_record_lines)
            if not success:
                break
            cur_record_lines = []
        else:
            cur_record_lines.append(line)

    # only parse final record if we've been successful so far; i.e., die
    # on the first failed parse
    if success:
        success = process_record(cur_record_lines)

    # now do some filtering action based on heuristics
    filtered_execution_points = []

    for pt in all_execution_points:
        # any execution point with a 0x0 frame pointer is bogus
        frame_ids = [e['frame_id'] for e in pt['stack_to_render']]
        func_names = [e['func_name'] for e in pt['stack_to_render']]
        if '0x0' in frame_ids:
            continue

        # any point with DUPLICATE frame_ids is bogus, since it means
        # that the frame_id of some frame hasn't yet been updated
        if len(set(frame_ids)) < len(frame_ids):
            continue

        # any point with a weird '???' function name is bogus
        # but we shouldn't have any more by now
        assert '???' not in func_names

        # print func_names, frame_ids
        filtered_execution_points.append(pt)

    final_execution_points = []
    if filtered_execution_points:
        final_execution_points.append(filtered_execution_points[0])
        # finally, make sure that each successive entry contains
        # frame_ids that are either identical to the previous one, or
        # differ by the addition or subtraction of one element at the
        # end, which represents a function call or return, respectively.
        # there are weird cases like:
        #
        # [u'main'] [u'0xFFEFFFE30']
        # [u'main'] [u'0xFFEFFFE30']
        # [u'foo'] [u'0xFFEFFFDC0'] <- bogus
        # [u'main', u'foo'] [u'0xFFEFFFE30', u'0xFFEFFFDC0']
        # [u'main', u'foo'] [u'0xFFEFFFE30', u'0xFFEFFFDC0']
        #
        # where the middle entry should be FILTERED OUT since it's
        # missing 'main' for some reason
        for prev, cur in zip(filtered_execution_points, filtered_execution_points[1:]):
            prev_frame_ids = [e['frame_id'] for e in prev['stack_to_render']]
            cur_frame_ids = [e['frame_id'] for e in cur['stack_to_render']]

            # identical, we're good to go
            if prev_frame_ids == cur_frame_ids:
                final_execution_points.append(cur)
            elif len(prev_frame_ids) < len(cur_frame_ids):
                # cur_frame_ids is prev_frame_ids + 1 extra element on
                # the end -> function call
                if prev_frame_ids == cur_frame_ids[:-1]:
                    final_execution_points.append(cur)
            elif len(prev_frame_ids) > len(cur_frame_ids):
                # cur_frame_ids is prev_frame_ids MINUS the last element on
                # the end -> function return
                if cur_frame_ids == prev_frame_ids[:-1]:
                    final_execution_points.append(cur)

        assert len(final_execution_points) <= len(filtered_execution_points)

        # now mark 'call' and' 'return' events via the same heuristic as above
        for prev, cur in zip(final_execution_points, final_execution_points[1:]):
            prev_frame_ids = [e['frame_id'] for e in prev['stack_to_render']]
            cur_frame_ids = [e['frame_id'] for e in cur['stack_to_render']]

            if len(prev_frame_ids) < len(cur_frame_ids):
                if prev_frame_ids == cur_frame_ids[:-1]:
                    cur['event'] = 'call'
            elif len(prev_frame_ids) > len(cur_frame_ids):
                if cur_frame_ids == prev_frame_ids[:-1]:
                    prev['event'] = 'return'

        # make the last statement a faux 'return', presumably from main
        if success:
            final_execution_points[-1]['event'] = 'return'

    # only keep the FIRST 'step_line' event for any given line, to match what
    # a line-level debugger would do
    if ONLY_ONE_REC_PER_LINE:
        tmp = []
        prev_event = None
        prev_line = None
        prev_frame_ids = None

        for elt in final_execution_points:
            skip = False
            cur_event = elt['event']
            cur_line = elt['line']
            cur_frame_ids = [e['frame_id'] for e in elt['stack_to_render']]
            if prev_frame_ids:
                if cur_event == prev_event == 'step_line':
                    if cur_line == prev_line and cur_frame_ids == prev_frame_ids:
                        skip = True

            if not skip:
                tmp.append(elt)

            prev_event = cur_event
            prev_line = cur_line
            prev_frame_ids = cur_frame_ids

        final_execution_points = tmp  # the ole' switcheroo

    if len(final_execution_points) > MAX_STEPS:
        # truncate to MAX_STEPS entries
        final_execution_points = final_execution_points[:MAX_STEPS]
        final_execution_points[-1]['event'] = 'instruction_limit_reached'
        final_execution_points[-1]['exception_msg'] = 'Stopped after running ' + str(
            MAX_STEPS) + ' steps. Please shorten your code,\nsince Python Tutor is not designed to handle long-running code.'

    '''
    for elt in final_execution_points:
        skip = False
        cur_event = elt['event']
        cur_line = elt['line']
        cur_frame_ids = [e['frame_id'] for e in elt['stack_to_render']]
        print cur_event, cur_line, cur_frame_ids
    '''

    cod = open(fn).read()
    # produce the final trace, voila!
    final_res = {'code': cod, 'trace': final_execution_points}

    # use sort_keys to get some sensible ordering on object keys
    if options.js_varname:
        s = json.dumps(final_res, indent=2, sort_keys=True)
        print 'var ' + options.js_varname + ' = ' + s + ';'
    elif options.jsondump:
        print json.dumps(final_res, sort_keys=True)
    elif options.prettydump:
        print json.dumps(final_res, indent=2, sort_keys=True)
    else:
        assert False
