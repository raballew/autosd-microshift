# autosd-microshift

Bootc (image mode) AutoSD image with MicroShift running in the QM partition,
built with a custom automotive kernel that includes `CONFIG_OPENVSWITCH=m`.
The image has an immutable `/usr` backed by composefs-overlay with
`verity=require` and is managed by the `bootc` CLI. Targets a virtual aarch64
QEMU exporter managed by Jumpstarter.

## Prerequisites

- [`caib`](https://gitlab.com/CentOS/automotive/src/automotive-image-builder)
  CLI in `$PATH`
- [`jmp`](https://jumpstarter.dev) CLI configured with access to the cluster
- A Jumpstarter client config selecting the QEMU exporter (default client
  alias used below: `pwallrab`)
- `oc` or `kubectl` for cluster access after boot
- A Red Hat pull secret saved at `microshift/pull-secret.json` (see
  `microshift/pull-secret.json.example`; obtain from
  https://console.redhat.com/openshift/install/pull-secret)

## One-time setup: SSH key

The image embeds the public key from `ssh-authorized-keys` so that root login
works without a password over the jmp SSH tunnel. Replace the file with your
own public key before building:

```bash
cat ~/.ssh/id_ed25519.pub > ssh-authorized-keys
```

## Step 1: Create the kernel build workspace

```bash
caib workspace create kernel-build \
  --cpu 30 \
  --memory 32Gi \
  --image quay.io/bzlotnik/autosd-toolchain:latest

# Wait until Running
caib workspace list
```

## Step 2: Build the custom kernel

Clone the kernel source and generate configs:

```bash
caib workspace exec kernel-build -- \
  "git clone --depth 1 --branch main \
    https://gitlab.com/redhat/centos-stream/src/kernel/centos-stream-10.git \
    /workspace/centos-stream-10"

caib workspace exec kernel-build -- \
  "cd /workspace/centos-stream-10 && \
   make AUTOMOTIVE_BUILD=1 dist-configs && \
   mkdir -p /tmp/build && \
   cp redhat/configs/kernel-automotive-6.12.0-aarch64.config /tmp/build/.config"
```

Enable `CONFIG_OPENVSWITCH=m` in the SRPM source config (not the test-compile
config — the SRPM must carry the change):

```bash
caib workspace exec kernel-build -- "
  sed -i 's/# CONFIG_OPENVSWITCH is not set/CONFIG_OPENVSWITCH=m/' \
    /workspace/centos-stream-10/redhat/configs/kernel-automotive-6.12.0-aarch64.config
"
```

Do a test compile to verify the config produces a working kernel (optional but
recommended; ~10 minutes with 30 CPUs):

```bash
caib workspace exec kernel-build -- \
  "cd /workspace/centos-stream-10 && \
   sed -i 's/# CONFIG_OPENVSWITCH is not set/CONFIG_OPENVSWITCH=m/' \
     /tmp/build/.config && \
   make mrproper && \
   make O=/tmp/build olddefconfig && \
   make O=/tmp/build -j30"
```

Build the source RPM, install it, and build binary RPMs:

```bash
caib workspace exec kernel-build -- \
  "cd /workspace/centos-stream-10 && \
   git checkout -- . && \
   sed -i 's/# CONFIG_OPENVSWITCH is not set/CONFIG_OPENVSWITCH=m/' \
     redhat/configs/kernel-automotive-6.12.0-aarch64.config && \
   make AUTOMOTIVE_BUILD=1 DISTLOCALVERSION=_custom dist-srpm"

caib workspace exec kernel-build -- \
  "rpm -ivh /workspace/centos-stream-10/redhat/rpm/SRPMS/kernel-automotive-*.src.rpm && \
   sed -i 's/^%define with_selftests 1/%define with_selftests 0/g' \
     /workspace/rpmbuild/SPECS/kernel-automotive.spec && \
   chmod -R g-s /workspace/rpmbuild"

caib workspace exec kernel-build -- \
  "rpmbuild -bb \
    --without debug --without debuginfo --without perf --without tools \
    --without configchecks \
    --define 'dist .el10iv' \
    --define '__spec_install_pre %{___build_pre}; \
      ln -sf usr/lib %{buildroot}/lib; \
      ln -sf usr/bin %{buildroot}/bin; \
      ln -sf usr/sbin %{buildroot}/sbin' \
    /workspace/rpmbuild/SPECS/kernel-automotive.spec"
```

Publish the RPMs as a DNF repo so the image builder can consume them:

```bash
caib workspace exec kernel-build -- \
  "mkdir -p /workspace/kernel-repo && \
   cp /workspace/rpmbuild/RPMS/aarch64/*.rpm /workspace/kernel-repo/ && \
   createrepo /workspace/kernel-repo"
```

Update `kernel_version` in `autosd-microshift.aib.yml` to match the built
RPM. The version is printed in the `Wrote:` line of the SRPM step, e.g.
`6.12.0-264_custom.el10iv`.

## Step 3: Build and flash the image

```bash
caib image build autosd-microshift.aib.yml \
  --extra-repo kernel-build:/workspace/kernel-repo \
  -a arm64 \
  --internal-registry \
  --disk \
  -D sign_kernel_modules=False \
  --flash \
  --exporter "target=qemu"
```

`-D sign_kernel_modules=False` skips the `kernel-automotive-devel` requirement
that AIB enables by default in bootc (image) mode for kernel-automotive packages.
The custom kernel RPMs are in the workspace repo but the osbuild depsolve runs in
the builder pod where the workspace HTTP server is not reachable.

On success the command prints a **Lease ID**. Keep it — every subsequent `jmp`
command needs it.

```
Lease ID: <lease-id>
```

## Step 4: Boot the device

```bash
export JMP_LEASE=<lease-id>
jmp shell -- j power on
```

The first-boot sequence (automatic, no intervention needed):

1. `http-timesync.service` — corrects the QEMU clock from a stale RTC value
   by fetching the `Date:` header from an HTTPS endpoint.
2. `chronyd.service` — maintains ongoing NTP sync once the clock is sane.
3. QM container starts; `microshift.service` comes up inside it.
4. `microshift-ovnk-patch.service` — patches OVN-Kubernetes DaemonSets to
   work with the custom kernel (no `nft_compat`, custom iptables wrappers).

MicroShift is ready in approximately 5 minutes.

## Step 5: Access MicroShift from localhost

Open a dedicated terminal and keep it running for the duration of your session:

```bash
export JMP_LEASE=<lease-id>
jmp shell -- j ssh -- -L 16443:localhost:6443 -N
```

This tunnels `localhost:16443` on your machine to port 6443 on the QEMU VM
(where QM publishes the MicroShift API).

In a second terminal, extract the kubeconfig and patch the server URL:

```bash
export JMP_LEASE=<lease-id>

jmp shell -- j ssh -- \
  'podman exec qm cat /var/lib/microshift/resources/kubeadmin/kubeconfig' \
  | grep -v '^\[' | grep -v '^Warning:' \
  | sed -n '/^apiVersion:/,$ p' \
  | sed 's|server: https://.*:6443|server: https://localhost:16443|' \
  > ~/.kube/microshift-config
```

Verify access:

```bash
oc --kubeconfig ~/.kube/microshift-config get nodes
oc --kubeconfig ~/.kube/microshift-config get pods -A
```

Expected output:

```
NAME           STATUS   ROLES                         AGE   VERSION
<hostname>     Ready    control-plane,master,worker   5m    v1.35.3

NAMESPACE                  NAME                                  READY   STATUS    RESTARTS
kube-system                csi-snapshot-controller-...           1/1     Running   0
openshift-dns              dns-default-...                       2/2     Running   0
openshift-dns              node-resolver-...                     1/1     Running   0
openshift-ingress          router-default-...                    1/1     Running   0
openshift-ovn-kubernetes   ovnkube-master-...                    4/4     Running   0
openshift-ovn-kubernetes   ovnkube-node-...                      1/1     Running   0
openshift-service-ca       service-ca-...                        1/1     Running   0
```

## Step 6: Release the lease when done

```bash
jmp shell -- j power off
jmp delete leases <lease-id>
```

## Running the automated tests

The test suite boots a local QEMU VM (no Jumpstarter required) and verifies
the full stack. Set `AUTOSD_DISK_IMAGE` to a locally built disk image or an
OCI reference:

```bash
# Build a local disk image first (no --flash)
caib image build autosd-microshift.aib.yml \
  --extra-repo kernel-build:/workspace/kernel-repo \
  -a arm64 \
  -D sign_kernel_modules=False

export AUTOSD_DISK_IMAGE=autosd-microshift.qcow2
cd tests && pytest -v
```

## Design notes

**Kernel: `CONFIG_OPENVSWITCH=m` added**: The stock `kernel-automotive` does
not enable Open vSwitch, which OVN-Kubernetes requires for the data-plane.
The kernel is rebuilt from `centos-stream-10` source with
`DISTLOCALVERSION=_custom` to add `CONFIG_OPENVSWITCH=m`.

**Kernel: no `nft_compat` — iptables replaced with nft wrappers**: The custom
kernel does not include `nft_compat.ko`, so the standard `iptables-nft` binary
fails at runtime. `microshift-ovnk-patch.service` fires once MicroShift's API
is up and patches the `ovnkube-master`, `ovnkube-node`, and `node-resolver`
DaemonSets to replace `iptables` calls with Python scripts that translate them
to direct `nft` commands.

**Bootc build: `sign_kernel_modules=False` required**: AIB defaults to
requiring `kernel-automotive-devel` in bootc mode to sign out-of-tree modules.
The osbuild depsolve runs inside the builder pod where the workspace HTTP
server hosting the custom kernel RPMs is not reachable, so the depsolve fails.
Passing `-D sign_kernel_modules=False` disables that requirement.

**QEMU clock: corrected before chronyd starts**: QEMU exporters boot with a
stale RTC (~2 weeks behind). TLS certificate validation fails at the wrong
timestamp, blocking image pulls inside QM. `http-timesync.service` fetches the
`Date:` header from an HTTPS endpoint with `curl -k` (skipping cert validation,
which is itself broken until the clock is fixed) and calls `date -s` before
`chronyd` takes over.

**QM: cgroupfs driver**: MicroShift and CRI-O are configured to use the
`cgroupfs` cgroup driver (`crio.conf.d/10-cgroupfs.conf`,
`microshift/config.yaml`). The systemd cgroup driver did not work inside the
QM container during bring-up; the root cause was not fully diagnosed.

**QM: `/proc/sys` remounted writable**: QM mounts `/proc/sys` read-only.
`remount-proc-sys.service` remounts it writable before CRI-O starts; without
this, `pinns` cannot set pod sysctls and every pod sandbox creation fails.

**QM: `CAP_SYS_RESOURCE` restored**: The QM base container drops
`CAP_SYS_RESOURCE`. Kubelet and privileged pods need it to write
`oom_score_adj` and call `capset`. `qm.container.d/20-microshift-caps.conf`
adds it back along with `/dev/kmsg` access.

**Firewalld FORWARD policy for Podman bridge**: Podman's Netavark backend adds
nftables accept rules at priority 0, but firewalld's FORWARD chains run at
priority 10 and reject unmatched traffic with `icmpx admin-prohibited`.
When Netavark's interface match does not cover the path from the QM bridge
(`10.88.0.0/16`) to `enp0s1`, DNS and image-pull traffic is dropped, leaving
all OVN-K pods in `ContainerCreating`. `firewalld/policies/podman-forward.xml`
adds an explicit firewalld policy that accepts this traffic within firewalld's
own processing order.
