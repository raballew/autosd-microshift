import os

import pytest
from jumpstarter.common.utils import serve
from jumpstarter_driver_qemu.driver import Qemu

# Accepts a local path or an OCI reference (oci://registry/repo:tag).
# caib image build … --push-disk quay.io/org/autosd-microshift-disk:latest
# sets AUTOSD_DISK_IMAGE=oci://quay.io/org/autosd-microshift-disk:latest
DISK_IMAGE = os.environ.get("AUTOSD_DISK_IMAGE", "autosd-microshift.qcow2")
ROOT_PASSWORD = os.environ.get("AUTOSD_ROOT_PASSWORD", "testpassword")
BOOT_TIMEOUT = int(os.environ.get("AUTOSD_BOOT_TIMEOUT", "600"))


@pytest.fixture(scope="session")
def running_vm():
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
                # maps host:16443 → VM:6443 → QM PublishPort → MicroShift API
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
