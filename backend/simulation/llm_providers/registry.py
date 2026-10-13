"""Compatibility import for existing Simulation evaluation scripts.
The runtime implementation lives in the backend.
"""

import importlib
import sys

sys.modules[__name__] = importlib.import_module("backend.app.services.llm_providers.registry")
