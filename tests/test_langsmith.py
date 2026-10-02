"""Tests for the LangSmith tracing integration and fallback wrapper."""

import unittest
from tracer_langsmith import get_langsmith_status, is_langsmith_enabled, traceable


class TestLangSmithTracing(unittest.TestCase):
    def test_status_structure(self):
        status = get_langsmith_status()
        self.assertIn("enabled", status)
        self.assertIn("project", status)
        self.assertIn("endpoint", status)
        import os
        expected_project = os.getenv("LANGSMITH_PROJECT", "stream-graph")
        self.assertEqual(status["project"], expected_project)

    def test_traceable_decorator_passthrough(self):
        @traceable(name="test_function", run_type="chain")
        def add(a: int, b: int) -> int:
            """Sample docstring."""
            return a + b

        self.assertEqual(add.__name__, "add")
        self.assertEqual(add.__doc__, "Sample docstring.")
        self.assertEqual(add(10, 20), 30)

    def test_traceable_with_kwargs(self):
        @traceable(name="sample_generator", run_type="tool")
        def sample_gen(items):
            for i in items:
                yield i * 2

        results = list(sample_gen([1, 2, 3]))
        self.assertEqual(results, [2, 4, 6])


if __name__ == "__main__":
    unittest.main()
