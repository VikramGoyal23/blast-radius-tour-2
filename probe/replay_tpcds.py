"""Replays single queries of tests/test_executor.py::TestExecutor::test_execute_tpcds.

That test is one test but 99 independent queries: each is parsed, shipped to a
worker process, executed there by sqlglot's Python executor, shipped back and
compared with what duckdb returns. Replaying a query does the same steps for
that query alone, in this process, with pickle standing in for the trip to
the worker and back. Everything sqlglot does comes from the test module's own
functions (mp_execute, cached_execute, subtestHelper), not from copies.

Used twice: tools/trace_tpcds.py traces which lines each query executes, and
the selector replays the queries a patch can reach before paying for the test.
"""

import pickle


def prepare():
    """What the test does before its queries: load the data, hand it over."""
    import tests.test_executor as module

    module.TestExecutor.setUpClass()
    case = module.TestExecutor("test_execute_tpcds")
    tables = pickle.loads(pickle.dumps(case.tpcds_tables))
    module.initializer(module.TPCDS_SCHEMA, tables)
    return module, case


def replay(module, case, index):
    """Runs query `index` the way the test does. Raises if it would fail."""
    meta, sql, _ = case.tpcds_sqls[index]
    expression = pickle.loads(pickle.dumps(module.parse_one(sql)))
    table = module.mp_execute(expression, meta)
    if table is not None:
        table = pickle.loads(pickle.dumps(table))
        case.subtestHelper(index, table, tpch=False)


def run(indices):
    """True if every listed query passes."""
    module, case = prepare()
    try:
        for index in indices:
            replay(module, case, index)
    except BaseException:  # noqa: BLE001
        return False
    return True
