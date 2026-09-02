#!/usr/bin/env python3
import json
import subprocess
import time

KUBECONFIG = "/var/lib/microshift/resources/kubeadmin/kubeconfig"
kubectl = ["kubectl", "--kubeconfig", KUBECONFIG]


def wait_for_api():
    while True:
        try:
            subprocess.check_call(
                kubectl + ["get", "nodes"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except subprocess.CalledProcessError:
            time.sleep(5)


def patch_ovnkube_master_sysctls():
    ds = json.loads(
        subprocess.check_output(
            kubectl
            + [
                "-n",
                "openshift-ovn-kubernetes",
                "get",
                "daemonset",
                "ovnkube-master",
                "-o",
                "json",
            ]
        )
    )
    containers = ds["spec"]["template"]["spec"]["containers"]
    idx = next(i for i, c in enumerate(containers) if c["name"] == "ovnkube-master")
    cmd = containers[idx]["command"]

    if len(cmd) < 3 or "ipv6" not in cmd[2]:
        return

    script = cmd[2]
    if "ipv6.conf.all.forwarding=1 || true" in script:
        return

    patched = script.replace(
        "sysctl net.ipv6.conf.all.forwarding=1\n",
        "sysctl net.ipv6.conf.all.forwarding=1 || true\n",
    ).replace(
        "sysctl net.ipv6.conf.default.forwarding=1\n",
        "sysctl net.ipv6.conf.default.forwarding=1 || true\n",
    )

    if patched == script:
        return

    patch = json.dumps(
        [
            {
                "op": "replace",
                "path": f"/spec/template/spec/containers/{idx}/command",
                "value": [cmd[0], cmd[1], patched],
            }
        ]
    )
    subprocess.check_call(
        kubectl
        + [
            "-n",
            "openshift-ovn-kubernetes",
            "patch",
            "daemonset",
            "ovnkube-master",
            "--type=json",
            f"--patch={patch}",
        ]
    )


wait_for_api()
patch_ovnkube_master_sysctls()
