#!/bin/bash
set -euo pipefail

KUBECONFIG=/var/lib/microshift/resources/kubeadmin/kubeconfig
KUBECTL="kubectl --kubeconfig ${KUBECONFIG}"

wait_for_api() {
    until ${KUBECTL} get nodes &>/dev/null 2>&1; do
        sleep 5
    done
}

wait_for_daemonset() {
    local namespace="$1"
    local daemonset="$2"
    until ${KUBECTL} -n "${namespace}" get daemonset "${daemonset}" &>/dev/null 2>&1; do
        sleep 5
    done
}

patch_ovnkube_master() {
    wait_for_daemonset openshift-ovn-kubernetes ovnkube-master
    local current_cmd
    current_cmd=$(${KUBECTL} -n openshift-ovn-kubernetes get daemonset ovnkube-master \
        -o jsonpath='{.spec.template.spec.containers[3].command[2]}' 2>/dev/null || echo "")

    if echo "${current_cmd}" | grep -q "nft add table ip raw"; then
        return 0
    fi

    python3 - <<'PYEOF'
import subprocess, json, sys

kubeconfig = "/var/lib/microshift/resources/kubeadmin/kubeconfig"
kubectl = ["kubectl", "--kubeconfig", kubeconfig]

ds_json = json.loads(subprocess.check_output(
    kubectl + ["-n", "openshift-ovn-kubernetes", "get", "daemonset", "ovnkube-master", "-o", "json"]
))
containers = ds_json["spec"]["template"]["spec"]["containers"]
idx = next(i for i, c in enumerate(containers) if c["name"] == "ovnkube-master")

startup_script = r"""set -xe
if [[ -f "/env/_master" ]]; then
  set -o allexport
  source "/env/_master"
  set +o allexport
fi
echo "$(date -Iseconds) - starting ovnkube-master, Node: ${K8S_NODE} IP: ${K8S_NODE_IP}"
echo "I$(date "+%m%d %H:%M:%S.%N") - copy ovn-k8s-cni-overlay"
cp -f /usr/libexec/cni/ovn-k8s-cni-overlay /cni-bin-dir/
echo "I$(date "+%m%d %H:%M:%S.%N") - disable conntrack on geneve port"
nft add table ip raw 2>/dev/null || true
nft add chain ip raw prerouting '{ type filter hook prerouting priority raw; }' 2>/dev/null || true
nft add chain ip raw output '{ type filter hook output priority raw; }' 2>/dev/null || true
nft add rule ip raw prerouting udp dport 6081 notrack 2>/dev/null || true
nft add rule ip raw output udp dport 6081 notrack 2>/dev/null || true
nft add table ip6 raw 2>/dev/null || true
nft add chain ip6 raw prerouting '{ type filter hook prerouting priority raw; }' 2>/dev/null || true
nft add chain ip6 raw output '{ type filter hook output priority raw; }' 2>/dev/null || true
nft add rule ip6 raw prerouting udp dport 6081 notrack 2>/dev/null || true
nft add rule ip6 raw output udp dport 6081 notrack 2>/dev/null || true
echo "I$(date "+%m%d %H:%M:%S.%N") - remount /proc/sys writable for sysctl operations"
mount -o remount,rw /proc/sys || true
echo "I$(date "+%m%d %H:%M:%S.%N") - starting ovnkube-node"
gateway_mode_flags="--gateway-mode local --gateway-interface br-ex"
sysctl net.ipv4.ip_forward=1 || true
sysctl net.ipv6.conf.all.forwarding=1 || true
sysctl net.ipv6.conf.default.forwarding=1 || true
gw_interface_flag=
if [ -d /sys/class/net/br-ex1 ]; then
  gw_interface_flag="--exgw-interface=br-ex1"
fi
echo "I$(date "+%m%d %H:%M:%S.%N") - ovnkube-master - start ovnkube ..."
exec /usr/bin/ovnkube \
  --init-cluster-manager "${K8S_NODE}" \
  --init-ovnkube-controller "${K8S_NODE}" \
  --init-node "${K8S_NODE}" \
  --allow-no-uplink \
  --config-file=/run/ovnkube-config/ovnkube.conf \
  --loglevel "${OVN_KUBE_LOG_LEVEL}" \
  ${gateway_mode_flags} \
  ${gw_interface_flag} \
  --inactivity-probe="180000" \
  --nb-address "" \
  --sb-address "" \
  --enable-multicast \
  --disable-snat-multiple-gws \
  --single-node \
  --acl-logging-rate-limit "20"
"""

new_volumes = [
    {"name": "iptables-wrapper", "hostPath": {"path": "/etc/microshift/bin/iptables-wrapper.sh", "type": "File"}},
    {"name": "ip6tables-wrapper", "hostPath": {"path": "/etc/microshift/bin/ip6tables-wrapper.sh", "type": "File"}},
    {"name": "iptables-nft-wrapper-py", "hostPath": {"path": "/etc/microshift/bin/iptables-nft-wrapper.py", "type": "File"}},
    {"name": "ip6tables-nft-wrapper-py", "hostPath": {"path": "/etc/microshift/bin/ip6tables-nft-wrapper.py", "type": "File"}},
]

new_mounts = [
    {"name": "iptables-wrapper", "mountPath": "/usr/sbin/iptables"},
    {"name": "ip6tables-wrapper", "mountPath": "/usr/sbin/ip6tables"},
    {"name": "iptables-nft-wrapper-py", "mountPath": "/var/iptables-nft-wrapper.py"},
    {"name": "ip6tables-nft-wrapper-py", "mountPath": "/var/ip6tables-nft-wrapper.py"},
]

patches = [
    {"op": "replace", "path": f"/spec/template/spec/containers/{idx}/command",
     "value": ["/bin/bash", "-c", startup_script]},
]
for vol in new_volumes:
    patches.append({"op": "add", "path": "/spec/template/spec/volumes/-", "value": vol})
for mount in new_mounts:
    patches.append({"op": "add",
                    "path": f"/spec/template/spec/containers/{idx}/volumeMounts/-",
                    "value": mount})

result = subprocess.run(
    kubectl + ["-n", "openshift-ovn-kubernetes", "patch", "daemonset", "ovnkube-master",
               "--type=json", f"--patch={json.dumps(patches)}"],
    capture_output=True, text=True
)
if result.returncode != 0:
    print(result.stderr, file=sys.stderr)
    sys.exit(1)
print(result.stdout)
PYEOF
}

patch_privileged_container() {
    local namespace="$1"
    local daemonset="$2"
    local container_index="$3"

    wait_for_daemonset "${namespace}" "${daemonset}"
    local is_privileged
    is_privileged=$(${KUBECTL} -n "${namespace}" get daemonset "${daemonset}" \
        -o jsonpath="{.spec.template.spec.containers[${container_index}].securityContext.privileged}" \
        2>/dev/null || echo "false")

    if [[ "${is_privileged}" != "true" ]]; then
        return 0
    fi

    ${KUBECTL} -n "${namespace}" patch daemonset "${daemonset}" \
        --type=json --patch="$(python3 -c "
import json
ALL_CAPS_EXCEPT_SYSBOOT = [
    'CHOWN', 'DAC_OVERRIDE', 'DAC_READ_SEARCH', 'FOWNER', 'FSETID', 'KILL',
    'SETGID', 'SETUID', 'SETPCAP', 'LINUX_IMMUTABLE', 'NET_BIND_SERVICE',
    'NET_BROADCAST', 'NET_ADMIN', 'NET_RAW', 'IPC_LOCK', 'IPC_OWNER',
    'SYS_MODULE', 'SYS_RAWIO', 'SYS_CHROOT', 'SYS_PTRACE', 'SYS_PACCT',
    'SYS_ADMIN', 'SYS_NICE', 'SYS_RESOURCE', 'SYS_TIME', 'SYS_TTY_CONFIG',
    'MKNOD', 'LEASE', 'AUDIT_WRITE', 'AUDIT_CONTROL', 'SETFCAP',
    'MAC_OVERRIDE', 'MAC_ADMIN', 'SYSLOG', 'WAKE_ALARM', 'BLOCK_SUSPEND',
    'AUDIT_READ', 'PERFMON', 'BPF', 'CHECKPOINT_RESTORE',
]
idx = ${container_index}
patches = [
    {'op': 'remove', 'path': f'/spec/template/spec/containers/{idx}/securityContext/privileged'},
    {'op': 'add', 'path': f'/spec/template/spec/containers/{idx}/securityContext/capabilities',
     'value': {'add': ALL_CAPS_EXCEPT_SYSBOOT}},
]
print(json.dumps(patches))
")"
}

patch_node_resolver() {
    wait_for_daemonset openshift-dns node-resolver
    local container_name
    container_name=$(${KUBECTL} -n openshift-dns get daemonset node-resolver \
        -o jsonpath='{.spec.template.spec.containers[0].name}' 2>/dev/null || echo "")

    local is_privileged
    is_privileged=$(${KUBECTL} -n openshift-dns get daemonset node-resolver \
        -o jsonpath='{.spec.template.spec.containers[0].securityContext.privileged}' 2>/dev/null || echo "false")

    if [[ "${is_privileged}" != "true" ]]; then
        return 0
    fi

    ${KUBECTL} -n openshift-dns patch daemonset node-resolver \
        --type=json --patch="$(python3 -c "
import json
caps = ['NET_ADMIN', 'SYS_ADMIN', 'NET_RAW', 'SYS_PTRACE', 'DAC_OVERRIDE']
patches = [
    {'op': 'remove', 'path': '/spec/template/spec/containers/0/securityContext/privileged'},
    {'op': 'add', 'path': '/spec/template/spec/containers/0/securityContext/capabilities',
     'value': {'add': caps}},
]
print(json.dumps(patches))
")"
}

wait_for_api
patch_ovnkube_master
# ovnkube-master container is at index 3 (after northd, nbdb, sbdb)
patch_privileged_container openshift-ovn-kubernetes ovnkube-master 3
# ovn-controller is the only container in ovnkube-node
patch_privileged_container openshift-ovn-kubernetes ovnkube-node 0
patch_node_resolver
