"""Run tests with an explicit fake transport when Unix sockets are unavailable.

This checks acquisition and control semantics, not the OS socket transport.
The ordinary unittest/pixi test command retains its real Unix-socket fake.
"""

import unittest

from tests import test_calibration_run
from tests.fakes import FakeCitrus, InProcessCitrusControl


def main():
    instances = {}

    def fake_server(**kwargs):
        fake = FakeCitrus(serve_socket=False, **kwargs)
        fake.socket_path = str(id(fake))
        instances[fake.socket_path] = fake
        return fake

    def fake_client(socket_path, **kwargs):
        return InProcessCitrusControl(instances[socket_path])

    test_calibration_run.FakeCitrus = fake_server
    test_calibration_run.CitrusControl = fake_client
    suite = unittest.defaultTestLoader.discover('tests', top_level_dir='.')
    return not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == '__main__':
    raise SystemExit(main())
