from __future__ import division

import datetime
import time
import traceback
import collections
import sys
import os
try:
    import queue
except ImportError:
    import Queue as queue

import mako.exceptions
import mako.template
import mako.lookup
import sqlalchemy.engine
import sqlalchemy.event
import sqlparse


REPORT_HTML = "html"
REPORT_WSGI = "wsgi"
REPORT_TEXT = "text"


def format_sql(sql):
    try:
        return sqlparse.format(sql, reindent=True)
    except Exception:
        return sql


class QueryStats(object):
    """ Statistics about a query

    You should not create these objects, but your application will receive
    a list of them as the result of a call to :func:`ProfilingSession.collect`
    You may wish to to inspect of filter them before passing them into
    :func:`sqltap.report`.

    :attr text: The text of the query
    :attr stack: The stack trace when this query was issued. Formatted as
        returned by py:func:`traceback.extract_stack`
    :attr duration: Duration of the query in seconds.
    :attr user_context: The value returned by the user_context_fn set
        with :func:`sqltap.start`.
    """
    def __init__(self, text, stack, start_time, end_time,
                 user_context, params_dict, results):
        self.text = text
        self.params = params_dict
        self.params_id = None
        self.stack = stack
        self.start_time = start_time
        self.end_time = end_time
        self.duration = end_time - start_time
        self.user_context = user_context
        self.rowcount = results.rowcount
        self.params_hash = self._calculate_params_hash(self.params)

    def _calculate_params_hash(self, params):
        h = 0
        for k in sorted(params.iterkeys()):
            h ^= 10009 * hash(params[k])
        return (h ^ (h >> 32)) & ((1 << 32) - 1)  # convert to 32-bit unsigned

    def __repr__(self):
        return ("<%s text='%s...' params=%r "
                "duration=%.3f rowcount=%d params_hash=%08x>" % (
                    self.__class__.__name__, str(self.text)[:40], self.params,
                    self.duration, self.rowcount, self.params_hash))


class ProfilingSession(object):
    """ A ProfilingSession captures queries run on an Engine and metadata about
    them.

    The profiling session hooks into SQLAlchmey and captures query text,
    timing information, and backtraces of where those queries came from.

    You may have multiple profiling sessions active at the same time on
    the same or different Engines. If multiple profiling sessions are
    active on the same engine, queries on that engine will be collected
    by both sessions.

    You may pass a context function to the session's constructor which
    will be executed at each query invocation and its result stored with
    that query. This is useful for associating queries with specific
    requests in a web framework, or specific threads in a process.

    By default, a session collects all of :class:`QueryStats` objects in
    an internal queue whose contents you can retrieve by calling
    :func:`ProfilingSession.collect`. If you want to collect the query
    results continually, you may do so by passing your own collection
    function to the session's constructor.

    You may start, stop, and restart a profiling session as much as you
    like. Calling start on an already started session or stop on an
    already stopped session will raise an :class:`AssertionError`.

    You may use a profiling session object like a context manager. This
    has the effect of only profiling queries issued while executing
    within the context.

    Example usage::

        profiler = ProfilingSession()
        with profiler:
            for number in Session.query(Numbers).filter(Numbers.value <= 3):
                print number

    You may also use a profiling session object like a decorator. This
    has the effect of only profiling queries issued within the decorated
    function.

    Example usage::

        profiler = ProfilingSession()

        @profiler
        def holy_hand_grenade():
            for number in Session.query(Numbers).filter(Numbers.value <= 3):
                print number
    """

    def __init__(self, engine=sqlalchemy.engine.Engine, user_context_fn=None,
                 collect_fn=None):
        """ Create a new :class:`ProfilingSession` object

        :param engine: The sqlalchemy engine on which you want to
            profile queries. The default is sqlalchemy.engine.Engine
            which will profile queries across all engines.

        :param user_context_fn: A function which returns a value to be stored
            with the query statistics. The function takes the same parameters
            passed to the after_execute event in sqlalchemy:
            (conn, clause, multiparams, params, results)

        :param collect_fn: A function which accepts a :class:`QueryStats`
            argument. If specified, the :class:`ProfilingSession` will not
            save queries in an internal queue and will instead pass them
            to this function immediately.
        """
        self.started = False
        self.engine = engine
        self.user_context_fn = user_context_fn

        if collect_fn:
            # the user said they want to do their own collecting
            self.collector = None
            self.collect_fn = collect_fn
        else:
            # we're doing the collecting, make an unbounded thread-safe queue
            self.collector = queue.Queue(0)
            self.collect_fn = self.collector.put

    def _before_exec(self, conn, clause, multiparams, params):
        """ SQLAlchemy event hook """
        conn._sqltap_query_start_time = time.time()

    def _after_exec(self, conn, clause, multiparams, params, results):
        """ SQLAlchemy event hook """
        # calculate the query time
        end_time = time.time()
        start_time = getattr(conn, '_sqltap_query_start_time', end_time)

        # get the user's context
        context = (None if not self.user_context_fn else
                   self.user_context_fn(conn, clause,
                                        multiparams, params, results))

        try:
            text = clause.compile(dialect=conn.engine.dialect)
        except AttributeError:
            text = clause

        params_dict = self._extract_parameters_from_results(results)

        stack = traceback.extract_stack()[:-1]
        qstats = QueryStats(text, stack, start_time, end_time,
                            context, params_dict, results)

        self.collect_fn(qstats)

    def _extract_parameters_from_results(self, query_results):
        params_dict = {}
        for p in getattr(query_results.context, 'compiled_parameters', []):
            params_dict.update(p)
        return params_dict

    def collect(self):
        """ Return all queries collected by this profiling session so far.
        Throws an exception if you passed a `collect_fn` argument to the
        session's constructor.
        """
        if not self.collector:
            raise AssertionError("Can't call collect when you've registered "
                                 "your own collect_fn!")

        queries = []
        try:
            while True:
                queries.append(self.collector.get(block=False))
        except queue.Empty:
            pass

        return queries

    def start(self):
        """ Start profiling

        :raises AssertionError: If calling this function when the session
            is already started.
        """
        if self.started is True:
            raise AssertionError("Profiling session is already started!")

        self.started = True
        sqlalchemy.event.listen(self.engine, "before_execute",
                                self._before_exec)
        sqlalchemy.event.listen(self.engine, "after_execute", self._after_exec)

    def stop(self):
        """ Stop profiling

        :raises AssertionError: If calling this function when the session
            is already stopped.
        """
        if self.started is False:
            raise AssertionError("Profiling session is already stopped")

        self.started = False
        sqlalchemy.event.remove(self.engine, "before_execute",
                                self._before_exec)
        sqlalchemy.event.remove(self.engine, "after_execute", self._after_exec)

    def __enter__(self, *args, **kwargs):
        """ context manager """
        self.start()
        return self

    def __exit__(self, *args, **kwargs):
        """ context manager """
        self.stop()

    def __call__(self, fn):
        """ decorator """
        def decorated(*args, **kwargs):
            with self:
                return fn(*args, **kwargs)
        return decorated


class QueryGroup(object):
    """ A QueryGroup stores profiling statistics data on a set of similar
    queries, including their query text/time/count, backtrace stacks.
    """

    ParamsID = 1

    def __init__(self):
        self.queries = []
        self.stacks = collections.defaultdict(int)
        self.params_hashes = {}
        self.callers = {}
        self.max = 0
        self.min = sys.maxsize
        self.sum = 0
        self.rowcounts = 0
        self.mean = 0
        self.median = 0

    def find_user_fn(self, stack):
        """ rough heuristic to try to figure out what user-defined func
            in the call stack (i.e. not sqlalchemy) issued the query
        """
        for frame in reversed(stack):
            # frame[0] is the file path to the module
            if 'sqlalchemy' not in frame[0]:
                return frame

    def add(self, q):
        if not bool(self.queries):
            self.text = str(q.text)
            self.formatted_text = format_sql(self.text)
            self.first_word = self.text.split()[0]
        self.queries.append(q)
        self.stacks[q.stack_text] += 1
        self.callers[q.stack_text] = self.find_user_fn(q.stack)

        count, params_id, params = self.params_hashes.get(
            q.params_hash, (0, None, q.params))
        if params_id is None:
            self.__class__.ParamsID += 1
            params_id = self.ParamsID
        self.params_hashes[q.params_hash] = (count + 1, params_id, params)
        q.params_id = q.params_id or params_id

        self.max = max(self.max, q.duration)
        self.min = min(self.min, q.duration)
        self.sum += q.duration
        self.rowcounts += q.rowcount
        self.mean = self.sum / len(self.queries)

    def calc_median(self):
        queries = sorted(self.queries, key=lambda q: q.duration,
                         reverse=True)
        length = len(queries)
        if not length % 2:
            x1 = queries[length // 2].duration
            x2 = queries[length // 2 - 1].duration
            self.median = (x1 + x2) / 2
        else:
            self.median = queries[length // 2].duration


class Reporter(object):
    """ An SQLTap Reporter base class """

    REPORT_TITLE = "SQLTap Profiling Report"

    def __init__(self, stats, report_file=None, report_dir=".",
                 template_file=None, template_dir=None, **kwargs):
        """ Create a new :class:`Reporter` object

        :param stats: An iterable of :class:`QueryStats` objects over
            which to prepare a report. This is typically a list returned by
            a call to :func:`collect`.

        :param report_file: If present, additionally write the SQLTap report
            out to a file at the specified file.

        :param report_dir: If present, additionally write the SQLTap report
            out to a file under the specified folder.

        :param template_file: filename of the template to generate the report.

        :param template_dir: folder of the template to generate the report.
        """
        self.duration = ((stats[-1].end_time - stats[0].start_time)
                         if stats else 0)
        self.stats = stats
        self.report_file = report_file
        self.report_dir = report_dir
        self.template_file = template_file
        self.template_dir = template_dir
        self.kwargs = kwargs

        self._process_stats()

    def render(self, ex_handler=mako.exceptions.html_error_template):
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = self.template.render(
                query_groups=self._query_groups,
                all_group=self._all_group,
                report_title=self.REPORT_TITLE,
                report_time=current_time,
                duration=self.duration,
                **self.kwargs)
        except Exception:
            return ex_handler().render()
        return result

    def report(self, log_mode='w'):
        content = self.render()

        if self.report_file:
            report_file = os.path.join(self.report_dir, self.report_file)
            with open(report_file, log_mode) as f:
                f.write(content)

        return content

    def _init_template(self, template_filters=['unicode', 'h']):
        # create the template lookup
        if self.template_file is None:
            raise Exception("SQLTap Report template file not specified!")

        # (we need this for extensions inheriting the base template)
        if self.template_dir is None:
            self.template_dir = os.path.join(os.path.dirname(__file__),
                                             "templates")

        # mako fixes unicode -> str on py3k
        lookup = mako.lookup.TemplateLookup(self.template_dir,
                                            default_filters=template_filters)
        self.template = lookup.get_template(self.template_file)

    def _process_stats(self):
        """ Process query statistics

        Generate sorted :class:`QueryGroup` in :param:self._query_groups and
        all-in-one :class:`QueryGroup` in :param:self._all_group
        """
        query_groups = collections.defaultdict(QueryGroup)
        all_group = QueryGroup()

        # group together statistics for the same query
        for qstats in self.stats:
            qstats.stack_text = \
                ''.join(traceback.format_list(qstats.stack)).strip()

            group = query_groups[str(qstats.text)]
            group.add(qstats)
            all_group.add(qstats)

        query_groups = sorted(query_groups.values(), key=lambda g: g.sum,
                              reverse=True)

        # calculate the median for each group
        for g in query_groups:
            g.calc_median()

        self._query_groups = query_groups
        self._all_group = all_group


class HTMLReporter(Reporter):
    """ A SQLTap Reporter that generates HTML format reports """

    def __init__(self, stats, report_file=None, report_dir=".",
                 template_file="html.mako", template_dir=None, **kwargs):
        super(HTMLReporter, self).__init__(
            stats,
            report_file=report_file,
            report_dir=report_dir,
            template_file=template_file,
            template_dir=template_dir,
            **kwargs)

        self._init_template(template_filters=['unicode', 'h'])


class WSGIReporter(HTMLReporter):
    """ A SQLTap Reporter that generates WSGI format reports """

    def __init__(self, stats, report_file=None, report_dir=".",
                 template_file="wsgi.mako", template_dir=None, **kwargs):
        super(WSGIReporter, self).__init__(
            stats,
            report_file=report_file,
            report_dir=report_dir,
            template_file=template_file,
            template_dir=template_dir,
            **kwargs)


class TextReporter(Reporter):
    """ A SQLTap Reporter that generates text format reports """

    def __init__(self, stats, report_file=None, report_dir=".",
                 template_file="text.mako", template_dir=None, **kwargs):
        super(TextReporter, self).__init__(
            stats,
            report_file=report_file,
            report_dir=report_dir,
            template_file=template_file,
            template_dir=template_dir,
            **kwargs)

        self._init_template(template_filters=['unicode'])

    def render(self):
        return super(TextReporter, self).render(
            ex_handler=mako.exceptions.text_error_template)

    def report(self):
        return super(TextReporter, self).report(log_mode='a')


def start(engine=sqlalchemy.engine.Engine, user_context_fn=None,
          collect_fn=None):
    """ Create a new :class:`ProfilingSession` and call start on it.

    This is a convenience method. See :class:`ProfilingSession`'s
    constructor for documentation on the arguments.

    :return: A new :class:`ProfilingSession`
    """
    session = ProfilingSession(engine, user_context_fn, collect_fn)
    session.start()
    return session


def report(statistics, filename=None, template="html.mako", **kwargs):
    """ Generate an HTML report of query statistics.

    :param statistics: An iterable of :class:`QueryStats` objects over
        which to prepare a report. This is typically a list returned by
        a call to :func:`collect`.

    :param filename: If present, additionally write the report out to a file at
        the specified path.

    :param template: The name of the file in the sqltap/templates directory to
        render for the report. This is mostly intended for extensions to sqltap
        (like the wsgi extension). Not working when :param:`report_format`
        specified.

    :param report_format: (Optional) Choose the format for SQLTap report,
        candidates are ["html", "wsgi", "text"]

    :return: The generated SQLTap Report.
    """
    REPORTER_MAPPING = {REPORT_HTML: HTMLReporter,
                        REPORT_WSGI: WSGIReporter,
                        REPORT_TEXT: TextReporter}

    report_format = kwargs.get('report_format')
    if report_format:
        report_format = report_format.lower()

        if report_format not in REPORTER_MAPPING.keys():
            raise Exception("Format |%s| is not valid! formats supported: %s ",
                            (report_format, REPORTER_MAPPING.keys()))

        reporter = REPORTER_MAPPING[report_format](
            statistics, report_file=filename, **kwargs)
    else:
        reporter = HTMLReporter(
            statistics, report_file=filename, template_file=template, **kwargs)

    result = reporter.report()
    return result


def _hotfix_dispatch_remove():
    """ The fix for this bug is in sqlalchemy 0.9.4, until then, we'll
    monkey patch SQLalchemy so that it works """
    import sqlalchemy

    if sqlalchemy.__version__ >= "0.9.4":
        return

    from sqlalchemy.event.attr import _DispatchDescriptor
    from sqlalchemy.event import registry

    def remove(self, event_key):
        target = event_key.dispatch_target
        stack = [target]
        while stack:
            cls = stack.pop(0)
            stack.extend(cls.__subclasses__())
            if cls in self._clslevel:
                self._clslevel[cls].remove(event_key._listen_fn)
        registry._removed_from_collection(event_key, self)

    _DispatchDescriptor.remove = remove

_hotfix_dispatch_remove()
