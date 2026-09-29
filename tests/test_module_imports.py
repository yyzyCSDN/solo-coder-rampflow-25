import importlib
import pkgutil
import unittest

import rampflow


class ModuleImportTests(unittest.TestCase):
    def test_every_packaged_module_imports(self):
        failures = []
        modules = sorted(info.name for info in pkgutil.walk_packages(rampflow.__path__, "rampflow."))
        for name in modules:
            try:
                importlib.import_module(name)
            except Exception as error:  # pragma: no cover - reported as one useful failure
                failures.append(f"{name}: {type(error).__name__}: {error}")
        self.assertGreaterEqual(len(modules), 50)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
