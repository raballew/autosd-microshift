#!/bin/bash
# Annotates namespaces with openshift.io/sa.scc.uid-range as they are created.
# Works around a race in MicroShift 5.0.0-ec.6 where infrastructure-services-manager
# tries to deploy service-ca before the namespace-security-allocation-controller
# has had time to annotate the namespace.

KUBECONFIG=/var/lib/microshift/resources/kubeadmin/kubeconfig

until curl -sk https://localhost:6443/readyz >/dev/null 2>&1; do
    sleep 0.5
done

# Allocate a UID range block starting at 1000670000 (standard OCP range for
# openshift-service-ca; the namespace-security-allocation-controller will
# reconcile this to the correct value on its next pass).
NAMESPACES=(
    openshift-service-ca
    openshift-route-controller-manager
    openshift-controller-manager
    openshift-infra
    kube-system
    default
)

for ns in "${NAMESPACES[@]}"; do
    start=$((1000670000 + RANDOM % 1000 * 10000))
    kubectl --kubeconfig="$KUBECONFIG" annotate namespace "$ns" \
        "openshift.io/sa.scc.uid-range=${start}/10000" \
        --overwrite 2>/dev/null || true
done

# Keep watching for new unannotated namespaces
while true; do
    sleep 5
    kubectl --kubeconfig="$KUBECONFIG" get namespaces \
        -o jsonpath='{range .items[?(!@.metadata.annotations.openshift\.io/sa\.scc\.uid-range)]}{.metadata.name}{"\n"}{end}' \
        2>/dev/null | while read -r ns; do
        [[ -z "$ns" ]] && continue
        start=$((1000670000 + RANDOM % 1000 * 10000))
        kubectl --kubeconfig="$KUBECONFIG" annotate namespace "$ns" \
            "openshift.io/sa.scc.uid-range=${start}/10000" \
            --overwrite 2>/dev/null || true
    done
done
