"""K8S operator constants for labels and annotations."""


class K8sConstants:
    """Constants for K8S BatchSandbox labels and annotations."""

    # CRD configuration
    CRD_GROUP = "sandbox.opensandbox.io"
    CRD_VERSION = "v1alpha1"
    CRD_PLURAL = "batchsandboxes"
    CRD_KIND = "BatchSandbox"
    CRD_API_VERSION = f"{CRD_GROUP}/{CRD_VERSION}"  # sandbox.opensandbox.io/v1alpha1

    # Annotation keys
    ANNOTATION_ENDPOINTS = "sandbox.opensandbox.io/endpoints"
    ANNOTATION_PORTS = "rock.sandbox/ports"

    # Label keys
    LABEL_SANDBOX_ID = "rock.sandbox/sandbox-id"
    LABEL_RESOURCE_SPEEDUP = "batchsandbox.alibabacloud.com/resource-speedup"
    LABEL_TEMPLATE = "rock.sandbox/template"

    # Extension keys for DockerDeploymentConfig.extended_params
    EXT_POOL_NAME = "pool_name"
    EXT_TEMPLATE_NAME = "template_name"
    EXT_RESOURCE_VERSION = "k8s_resource_version"

    # Built-in template names
    TEMPLATE_DEFAULT = "default"

    # Nacos config keys
    NACOS_POOLS_KEY = "pools"
    NACOS_TEMPLATE_RULES_KEY = "template_rules"

    # Pool CRD (Warm path: Template API creates Pool CRD)
    CRD_PLURAL_POOL = "pools"
    CRD_KIND_POOL = "Pool"

    # Label: distinguish Template API created Pools from system-configured Pools
    LABEL_MANAGED_BY = "rock.sandbox/managed-by"
    LABEL_MANAGED_BY_TEMPLATE_API = "template-api"

    # templateID prefix
    TEMPLATE_ID_PREFIX = "tpl-"
