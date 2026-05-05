"""Tests for the __version__ constant in launch.py."""
import importlib
import types


def _get_launch_module() -> types.ModuleType:
    import launch
    importlib.reload(launch)
    return launch


def test_version_constant_exists():
    launch = _get_launch_module()
    assert hasattr(launch, "__version__"), "__version__ is not defined in launch.py"


def test_version_constant_value():
    launch = _get_launch_module()
    assert launch.__version__ == "4.0.0"


def test_version_constant_is_string():
    launch = _get_launch_module()
    assert isinstance(launch.__version__, str)


def test_version_matches_private_version():
    launch = _get_launch_module()
    assert launch.__version__ == launch._VERSION
