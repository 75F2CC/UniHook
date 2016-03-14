import os
import json
from sea_runtool import TaskCombiner, Progress, resolve_stack, to_hex, ProgressConst

MAX_GT_SIZE = 50 * 1024 * 1024
GT_FLOAT_TIME = False


class GoogleTrace(TaskCombiner):
    def __init__(self, args, tree):
        TaskCombiner.__init__(self, tree)
        self.args = args
        self.target_scale_start = 0
        self.source_scale_start = 0
        self.ratio = 1 / 1000.  # nanoseconds to microseconds
        self.size_keeper = None
        self.targets = []
        self.trace_number = 0
        self.counters = {}
        self.frames = {}
        self.samples = []
        self.last_task = None
        if self.args.trace:
            if self.args.trace.endswith(".etl"):
                self.handle_etw_trace(self.args.trace)
            else:
                self.args.sync = self.handle_ftrace(self.args.trace)
        self.start_new_trace()

    def start_new_trace(self):
        self.targets.append("%s-%d.json" % (self.args.output, self.trace_number))
        self.trace_number += 1
        self.file = open(self.targets[-1], "w")
        self.file.write('{')
        if self.args.sync:
            self.apply_time_sync(self.args.sync)
        self.file.write('\n"traceEvents": [\n')

        for key, value in self.tree["threads"].iteritems():
            pid_tid = key.split(',')
            self.file.write(
                '{"name": "thread_name", "ph":"M", "pid":%s, "tid":%s, "args": {"name":"%s(%s)"}},\n' % (pid_tid[0], pid_tid[1], value, pid_tid[1])
            )

    def get_targets(self):
        return self.targets

    def convert_time(self, time):
        return (time - self.source_scale_start) * self.ratio + self.target_scale_start

    @staticmethod
    def read_ftrace_lines(trace, time_sync):
        write_chrome_time_sync = True
        with open(trace) as file:
            count = 0
            with Progress(os.path.getsize(trace), 50, "Loading ftrace") as progress:
                for line in file:
                    if 'IntelSEAPI_Time_Sync' in line:
                        parts = line.split()
                        time_sync.append((float(parts[-4].strip(":")), int(parts[-1]))) #target (ftrace), source (nanosecs)
                        if write_chrome_time_sync:  # chrome time sync, pure zero doesn't work, so we shift on very little value
                            yield "%strace_event_clock_sync: parent_ts=%s\n" % (line.split("IntelSEAPI_Time_Sync")[0], line.split(":")[-4].split()[-1])
                            write_chrome_time_sync = False  # one per trace is enough
                    else:
                        yield line
                    if count % ProgressConst == 0:
                        progress.tick(file.tell())
                    count += 1

    def handle_ftrace(self, trace):
        time_sync = []
        self.targets.append(self.args.output + '.cut.ftrace')
        with open(self.targets[-1], 'w') as file:
            for line in GoogleTrace.read_ftrace_lines(trace, time_sync):
                if line.startswith('#') or 0 < len(time_sync) < 10: #we don't need anything outside proc execution but comments
                    file.write(line)
        return time_sync

    def handle_etw_trace(self, trace):
        assert (not "Implemented")

    def apply_time_sync(self, time_sync):
        if len(time_sync) < 2:  # too few markers to sync
            return
        Target = 0
        Source = 1
        # looking for closest time points to calculate start points
        diffs = []
        for i in range(1, len(time_sync)):
            diff = (time_sync[i][Target] - time_sync[i - 1][Target], time_sync[i][Source] - time_sync[i - 1][Source])
            diffs.append((diff, i))
        diffs.sort()
        (diff, index) = diffs[0]  # it's the width between two closest measurements

        # source measurement is the fisrt, target is second
        # Target time is always after the source, due to workflow
        # one measurement is begin -> begin and another is end -> end
        # if nothing interferes begin -> begin measurement should take same time as end -> end

        # run 1: most ballanced case - everything is even
        # S   /b  |  |  I  /e
        # T          /b  I  |  |  /e

        # run 2: takes more time after Target measurement
        # S   /b  |  |  I  /e
        # T      /b  I  |  |  /e

        # run 3: takes more time before Targer measurement
        # S   /b  |  |  I  /e
        # T              /b  I  |  |  /e

        # From these runs obvious that in all cases the closest points (I) of global timeline are:
        #   Quater to end of Source and Quater after begin of Target
        self.source_scale_start = time_sync[index - 1][Source] + int(diff[Source] * 0.75)  # to keep the precision
        self.target_scale_start = (time_sync[index - 1][Target] + (diff[Target] * 0.25)) * 1000000. #multiplying by 1000000. to have time is microseconds (ftrace/target time was in seconds)

        print "Timelines correlation precision is +- %f us" % (diff[Target] / 2. * 1000000.)

        # taking farest time points to calculate frequencies
        diff = (time_sync[-1][Target] - time_sync[0][Target], time_sync[-1][Source] - time_sync[0][Source])
        self.ratio = 1000000. * diff[Target] / diff[Source] # when you multiply Source value with this ratio you get Target units, multiplying by 1000000. to have time is microseconds (ftrace/target time was in seconds)

    def global_metadata(self, data):
        if data['str'] == "__process__":  # this is the very first record in the trace
            if data.has_key('data'):
                self.file.write(
                    '{"name": "process_name", "ph":"M", "pid":%d, "tid":%s, "args": {"name":"%s"}},\n' % (data['pid'], data['tid'], data['data'].replace("\\", "\\\\"))
                )
            if data.has_key('delta'):
                self.file.write(
                    '{"name": "process_sort_index", "ph":"M", "pid":%d, "tid":%s, "args": {"sort_index":%d}},\n' % (data['pid'], data['tid'], data['delta'])
                )
            if data['tid'] >= 0 and not self.tree['threads'].has_key('%d,%d' % (data['pid'], data['tid'])): #marking the main thread
                self.file.write(
                    '{"name": "thread_name", "ph":"M", "pid":%d, "tid":%s, "args": {"name":"%s"}},\n' % (data['pid'], data['tid'], "<main>")
                )

    def relation(self, data, head, tail):
        if not head or not tail:
            return
        items = sorted([head, tail], key=lambda item: item['time']) #we can't draw lines in backward direction, so we sort them by time
        if GT_FLOAT_TIME:
            template = '{"ph":"%s", "name": "relation", "pid":%d, "tid":%s, "ts":%.3f, "id":%s, "args":{"name": "%s"}, "cat":"%s"},\n'
        else:
            template = '{"ph":"%s", "name": "relation", "pid":%d, "tid":%s, "ts":%d, "id":%s, "args":{"name": "%s"}, "cat":"%s"},\n'
        if not data.has_key('str'):
            data['str'] = "unknown"
        self.file.write(template % ("s", items[0]['pid'], items[0]['tid'], self.convert_time(items[0]['time']), data['parent'], data['str'], data['domain']))
        self.file.write(template % ("f", items[1]['pid'], items[1]['tid'], self.convert_time(items[1]['time']), data['parent'], data['str'], data['domain']))

    def format_value(self, arg): #this function must add quotes if value is string, and not number/float, do this recursively for dictionary
        if type(arg) == type({}):
            return "{" + ", ".join(['"%s":%s' % (key, self.format_value(value)) for key, value in arg.iteritems()]) + "}"
        try:
            val = float(arg)
            if float('inf') != val:
                if val.is_integer():
                    return int(val)
                else:
                    return val
        except:
            pass
        return '"%s"' % str(arg).replace("\\", "\\\\").replace('"', '\\"')

    Phase = {'task':'X', 'counter':'C', 'marker':'i', 'object_new':'N', 'object_snapshot':'O', 'object_delete':'D', 'frame':'X'}

    def complete_task(self, type, begin, end):
        if self.args.distinct:
            if self.last_task == (type, begin, end):
                return
            self.last_task = (type, begin, end)
        assert (GoogleTrace.Phase.has_key(type))
        if begin['type'] == 7:  # frame_begin
            begin['id'] = begin['tid'] if begin.has_key('tid') else 0  # Async events are groupped by cat & id
            res = self.format_task('b', 'frame', begin, {})
            res += [',\n']
            end_begin = begin.copy()
            end_begin['time'] = end['time']
            res += self.format_task('e', 'frame', end_begin, {})
        else:
            res = self.format_task(GoogleTrace.Phase[type], type, begin, end)

        if not res:
            return
        if type in ['task', 'counter'] and begin.has_key('data') and begin.has_key('str'): #FIXME: move closer to the place where stack is demanded
            self.handle_stack(begin, resolve_stack(self.args, self.tree, begin['data']), begin['str'])
        if self.args.debug and begin['type'] != 7:
            res = "".join(res)
            try:
                json.loads(res)
            except Exception as exc:
                print "\n" + exc.message + ":\n" + res + "\n"
            res += ',\n'
        else:
            res = "".join(res + [',\n'])
        self.file.write(res)
        if (self.file.tell() > MAX_GT_SIZE):
            self.finish()
            self.start_new_trace()

    def handle_stack(self, task, stack, name='stack'):
        if not stack:
            return
        parent = None
        for frame in reversed(stack):  # going from parents to childs
            if parent == None:
                frame_id = '%d' % frame['ptr']
            else:
                frame_id = '%d:%s' % (frame['ptr'], parent)
            if not self.frames.has_key(frame_id):
                data = {'category': os.path.basename(frame['module']), 'name': frame['str']}
                if parent != None:
                    data['parent'] = parent
                self.frames[frame_id] = data
            parent = frame_id
        time = self.convert_time(task['time'])
        self.samples.append({
            'tid': task['tid'],
            'ts': time if GT_FLOAT_TIME else int(time),
            'sf': frame_id, 'name': name
        })

    Markers = {
        "unknown": "t",
        "global": "g",
        "track_group": "p",
        "track": "t",
        "task": "t",
        "marker": "t"
    }

    def format_task(self, phase, type, begin, end):
        res = []
        res.append('{"ph":"%s"' % (phase))
        res.append(', "pid":%(pid)d' % begin)
        if begin.has_key('tid'):
            res.append(', "tid":%(tid)d' % begin)
        if GT_FLOAT_TIME:
            res.append(', "ts":%.3f' % (self.convert_time(begin['time'])))
        else:
            res.append(', "ts":%d' % (self.convert_time(begin['time'])))
        if "counter" == type:  # workaround of chrome issue with forgetting the last counter value
            self.counters.setdefault(begin['domain'], {})[begin['str']] = begin  # remember the last counter value
        if "marker" == type:
            name = begin['str']
            res.append(', "s":"%s"' % (GoogleTrace.Markers[begin['data']]))
        elif "object_" in type:
            if begin.has_key('str'):
                name = begin['str']
            else:
                name = ""
        elif "frame" == type:
            if begin.has_key('str'):
                name = begin['str']
            else:
                name = begin['domain']
        else:
            if type not in ["counter", "task", "overlapped"]:
                name = type + ":"
            else:
                name = ""

            if begin.has_key('parent'):
                name += to_hex(begin['parent']) + "->"
            if begin.has_key('str'):
                name += begin['str'] + ":"
            if begin.has_key('pointer'):
                name += "func<" + to_hex(begin['pointer']) + ">:"
            if begin.has_key('id') and type != "overlapped":
                name += "(" + to_hex(begin['id']) + ")"
            else:
                name = name.rstrip(":")

        assert (name or "object_" in type)
        res.append(', "name":"%s"' % (name))
        res.append(', "cat":"%s"' % (begin['domain']))

        if begin.has_key('id'):
            res.append(', "id":%s' % (begin['id']))
        if type in ['task']:
            dur = self.convert_time(end['time']) - self.convert_time(begin['time'])
            if GT_FLOAT_TIME:
                res.append(', "dur":%.3f' % (dur))
            else:
                if dur < self.args.min_dur:
                    return []  # google misbehaves on tasks of 0 length
                res.append(', "dur":%d' % (dur))
        args = {}
        if begin.has_key('args'):
            args = begin['args'].copy()
        if end.has_key('args'):
            args.update(end['args'])
        if begin.has_key('__file__'):
            args["__file__"] = begin["__file__"]
            args["__line__"] = begin["__line__"]
        if 'counter' == type:
            args[name] = begin['delta']
        if begin.has_key('memory'):
            total = 0
            breakdown = {}
            children = 0
            for size, values in begin['memory'].iteritems():
                if size is None:  # special case for children attribution
                    children = values
                else:
                    all = sum(values)
                    total += size * all
                    if all:
                        breakdown[size] = all
            breakdown['TOTAL'] = total
            breakdown['CHILDREN'] = children
            args['CRT:Memory(size,count)'] = breakdown
        if args:
            res.append(', "args":')
            res.append(self.format_value(args))
        res.append('}');
        return res

    def handle_leftovers(self):
        TaskCombiner.handle_leftovers(self)
        for counters in self.counters.itervalues():  # workaround: google trace forgets counter last value
            for counter in counters.itervalues():
                counter['time'] += 1  # so, we repeat it on the end of the trace
                self.complete_task("counter", counter, counter)

    def finish(self):
        if self.samples:
            self.file.write('{}],\n"stackFrames":\n')
            self.file.write(json.dumps(self.frames))
            self.file.write(',\n"samples":\n')
            self.file.write(json.dumps(self.samples))
            self.file.write('}')
            self.samples = []
        else:
            self.file.write("{}]}")
        self.file.close()

    @staticmethod
    def join_traces(traces, output):
        import zipfile
        with zipfile.ZipFile(output + ".zip", 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zip:
            count = 0
            with Progress(len(traces), 50, "Merging traces") as progress:
                ftrace = []  # ftrace files have to be joint by time: chrome reads them in unpredictable order and complains about time
                for file in traces:
                    if file.endswith('.ftrace'):
                        if 'merged.ftrace' != os.path.basename(file):
                            ftrace.append(file)
                    else:
                        progress.tick(count)
                        zip.write(file, os.path.basename(file))
                        count += 1
                if len(ftrace) > 0:  # just concatenate all files in order of creation
                    if len(ftrace) == 1:
                        zip.write(ftrace[0], os.path.basename(ftrace[0]))
                    else:
                        ftrace.sort()  # name defines sorting
                        merged = os.path.join(os.path.dirname(ftrace[0]), 'merged.ftrace')
                        with open(merged, 'w') as output_file:
                            for file_name in ftrace:
                                with open(file_name) as input_file:
                                    for line in input_file.readlines():
                                        output_file.write(line)
                                progress.tick(count)
                                count += 1
                        zip.write(merged, os.path.basename(merged))
        return output + ".zip"

EXPORTER_DESCRIPTORS = [{
    'format': 'gt',
    'available': True,
    'exporter': GoogleTrace
}]