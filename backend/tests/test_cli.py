import pytest

from app.cli import main


@pytest.mark.parametrize("argv", [[], ["DELETE", "/jobs"], ["GET", "jobs"]])
def test_bad_args_exit_2(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 2
