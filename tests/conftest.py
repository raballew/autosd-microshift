import contextlib
import os

import fabric
import pytest

DISK_IMAGE = os.environ.get("AUTOSD_DISK_IMAGE", "autosd-microshift.qcow2")
ROOT_PASSWORD = os.environ.get("AUTOSD_ROOT_PASSWORD", "testpassword")
BOOT_TIMEOUT = int(os.environ.get("AUTOSD_BOOT_TIMEOUT", "600"))
SSH_HOST = os.environ.get("JMP_SSH_HOST", "localhost")
SSH_PORT = int(os.environ.get("JMP_SSH_PORT", "0"))


class _FabricVM:
    def __init__(self, conn: fabric.Connection) -> None:
        self._conn = conn

    @contextlib.contextmanager
    def shell(self):
        yield self._conn


@pytest.fixture(scope="session")
def running_vm():
    if SSH_PORT:
        conn = fabric.Connection(
            SSH_HOST,
            port=SSH_PORT,
            user="root",
            connect_kwargs={
                "password": ROOT_PASSWORD,
                "look_for_keys": False,
                "allow_agent": False,
            },
        )
        yield _FabricVM(conn)
        conn.close()
        return

    from jumpstarter.common.utils import serve
    from jumpstarter_driver_qemu.driver import Qemu

    with serve(
        Qemu(
            arch="aarch64",
            smp=4,
            mem="4G",
            disk_size="20G",
            username="root",
            password=ROOT_PASSWORD,
            hostfwd={
                "ssh": {"hostport": 2222, "guestport": 22},
                "apiserver": {"hostport": 16443, "guestport": 6443},
            },
        )
    ) as qemu:
        qemu.flasher.flash(DISK_IMAGE)
        qemu.power.on()

        with qemu.console.pexpect() as console:
            console.expect_exact("login:", timeout=BOOT_TIMEOUT)

        yield qemu

        qemu.power.off()
