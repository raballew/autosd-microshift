import ssl
import time
import urllib.request

import pytest

_MICROSHIFT_API = "https://127.0.0.1:16443"
_MICROSHIFT_READY_TIMEOUT = 300
_SERVICE_CHECK_TIMEOUT = 300
_KUBECONFIG = "/var/lib/microshift/resources/kubeadmin/kubeconfig"


def test_image_boots(running_vm):
    pass


def test_microshift_service_is_active(running_vm):
    with running_vm.shell() as shell:
        result = shell.run(
            f"timeout {_SERVICE_CHECK_TIMEOUT} bash -c "
            "'until systemctl is-active microshift.service 2>/dev/null"
            " | grep -q active; do sleep 10; done'",
            warn=True,
            hide=True,
        )
    assert result.return_code == 0, "microshift.service did not become active on the host"


def test_microshift_api_is_accessible_externally(running_vm):
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


def test_openvswitch_module_available(running_vm):
    with running_vm.shell() as shell:
        result = shell.run("modinfo openvswitch", warn=True, hide=True)
    assert result.return_code == 0, (
        "openvswitch kernel module not found — rebuild the kernel with CONFIG_OPENVSWITCH=m"
    )


def test_hostname_is_not_localhost(running_vm):
    with running_vm.shell() as shell:
        result = shell.run("hostname", warn=True, hide=True)
    assert result.stdout.strip() not in ("localhost", "localhost.localdomain"), (
        f"System hostname is '{result.stdout.strip()}' — MicroShift will reject localhost in apiserver SANs"
    )


def test_sshd_dns_lookup_disabled(running_vm):
    with running_vm.shell() as shell:
        result = shell.run(
            "grep -r 'UseDNS' /etc/ssh/sshd_config.d/ | grep -i 'no'",
            warn=True,
            hide=True,
        )
    assert result.return_code == 0, (
        "UseDNS no not found in sshd_config.d — reverse DNS lookups will delay SSH banner"
    )


def test_boot_marked_as_good(running_vm):
    with running_vm.shell() as shell:
        result = shell.run(
            "systemctl show ukiboot-set-success.service --property=ActiveState",
            warn=True,
            hide=True,
        )
    state = result.stdout.strip().removeprefix("ActiveState=")
    assert state == "active", (
        f"ukiboot-set-success.service is in state '{state}' — ukibootctl mark-successful did not run,"
        " tries_remaining will hit zero and VM will fail to boot after first power cycle"
    )


def test_boot_confirmed_before_microshift_starts(running_vm):
    with running_vm.shell() as shell:
        result = shell.run(
            "systemctl show ukiboot-set-success.service microshift.service"
            " --property=ActiveEnterTimestampMonotonic",
            warn=True,
            hide=True,
        )
    timestamps = [
        int(line.split("=", 1)[1])
        for line in result.stdout.strip().splitlines()
        if "=" in line
    ]
    assert len(timestamps) == 2, (
        "Could not read activation timestamps from both services"
    )
    ukiboot_ts, microshift_ts = timestamps
    assert ukiboot_ts < microshift_ts, (
        f"ukiboot-set-success activated at {ukiboot_ts}us, microshift at {microshift_ts}us —"
        " boot confirmation must run before microshift starts to survive early reboots"
    )


def test_greenboot_healthcheck_disabled(running_vm):
    with running_vm.shell() as shell:
        result = shell.run(
            "systemctl show greenboot-healthcheck.service --property=LoadState",
            warn=True,
            hide=True,
        )
    state = result.stdout.strip().removeprefix("LoadState=")
    assert state == "masked", (
        f"greenboot-healthcheck.service LoadState='{state}' — it must be masked;"
        " when health checks fail (MicroShift not ready at early boot), FailureAction triggers"
        " rapid reboot loop that makes the system unbootable"
    )


def test_ovn_networking_pods_running(running_vm):
    deadline = time.monotonic() + _MICROSHIFT_READY_TIMEOUT
    while time.monotonic() < deadline:
        with running_vm.shell() as shell:
            result = shell.run(
                f"kubectl --kubeconfig {_KUBECONFIG}"
                " get pods -n openshift-ovn-kubernetes"
                " --field-selector=status.phase=Running --no-headers 2>/dev/null"
                " | wc -l",
                warn=True,
                hide=True,
            )
        if result.return_code == 0 and int(result.stdout.strip() or "0") >= 2:
            return
        time.sleep(15)
    pytest.fail(
        f"OVN-Kubernetes pods did not reach Running state after {_MICROSHIFT_READY_TIMEOUT}s"
    )
