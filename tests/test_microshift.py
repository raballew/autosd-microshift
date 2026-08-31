import ssl
import time
import urllib.request

import pytest

_MICROSHIFT_API = "https://127.0.0.1:16443"
_MICROSHIFT_READY_TIMEOUT = 300
_SERVICE_CHECK_TIMEOUT = 300


def test_image_boots(running_vm):
    # Verified by the running_vm fixture: it only yields after the login prompt appears
    pass


def test_qm_service_is_active(running_vm):
    with running_vm.shell() as shell:
        result = shell.run("systemctl is-active qm.service", warn=True, hide=True)
    assert result.stdout.strip() == "active", (
        f"qm.service not active: {result.stdout.strip()!r}"
    )


def test_microshift_starts_in_qm(running_vm):
    # MicroShift starts after QM is up; poll until active or timeout
    with running_vm.shell() as shell:
        result = shell.run(
            f"timeout {_SERVICE_CHECK_TIMEOUT} bash -c "
            "'until podman exec qm systemctl is-active microshift.service 2>/dev/null"
            " | grep -q active; do sleep 10; done'",
            warn=True,
            hide=True,
        )
    assert result.return_code == 0, "microshift.service did not become active in QM"


def test_microshift_api_is_accessible_externally(running_vm):
    # Connects to localhost:16443 via the QEMU hostfwd chain:
    # host:16443 -> VM:6443 -> QM PublishPort -> MicroShift API server
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    deadline = time.monotonic() + _MICROSHIFT_READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"{_MICROSHIFT_API}/readyz", context=ctx, timeout=5
            ) as resp:
                if resp.read() == b"ok":
                    return
        except Exception:
            pass
        time.sleep(10)

    pytest.fail(
        f"MicroShift API at {_MICROSHIFT_API}/readyz did not return 'ok'"
        f" after {_MICROSHIFT_READY_TIMEOUT}s"
    )
