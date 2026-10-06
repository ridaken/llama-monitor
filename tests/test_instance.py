import pytest

from instance import backend_lock


def test_backend_lock_excludes_second_owner_and_releases(tmp_path):
    with backend_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already running"):
            with backend_lock(tmp_path):
                pass
    with backend_lock(tmp_path):
        pass
