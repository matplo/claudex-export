import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("check_release", Path(__file__).parents[1] / "scripts/check_release.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize("version", ["0.1.0", "1.2.3", "2.0.0rc1"])
def test_matching_release_tag(version):
    module.check_tag(f"v{version}", version)


@pytest.mark.parametrize("tag", ["0.1.0", "v0.2.0", "vlatest", "v0.1.0-extra"])
def test_incorrect_release_tag(tag):
    with pytest.raises(ValueError, match="does not match"):
        module.check_tag(tag, "0.1.0")
