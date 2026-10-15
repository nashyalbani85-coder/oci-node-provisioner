import os
import sys
import time
from datetime import datetime, timezone

import oci

DISPLAY_NAME = "Ampere-A1-Singapore"
SHAPE = "VM.Standard.A1.Flex"


def env(name, default=""):
    return os.getenv(name, default).strip()


user_id = env("OCI_USER_ID")
fingerprint = env("OCI_FINGERPRINT")
tenancy_id = env("OCI_TENANCY_ID")
region = env("OCI_REGION", "ap-singapore-2")
private_key = env("OCI_PRIVATE_KEY")
subnet_id = env("OCI_SUBNET_ID")
image_id = env("OCI_IMAGE_ID")
public_ssh_key = env("OCI_PUBLIC_SSH_KEY")

try:
    ocpus = float(env("OCI_OCPUS") or "2")
    memory_gbs = float(env("OCI_MEMORY_IN_GBS") or "12")
except ValueError:
    sys.exit("OCI_OCPUS and OCI_MEMORY_IN_GBS must be numbers.")

missing = [name for name, value in {
    "OCI_USER_ID": user_id,
    "OCI_FINGERPRINT": fingerprint,
    "OCI_TENANCY_ID": tenancy_id,
    "OCI_PRIVATE_KEY": private_key,
    "OCI_SUBNET_ID": subnet_id,
    "OCI_PUBLIC_SSH_KEY": public_ssh_key,
}.items() if not value]
if missing:
    sys.exit("Missing required settings: " + ", ".join(missing))

if not (0 < ocpus <= 2 and 1 <= memory_gbs <= 12):
    sys.exit("Refusing to launch: Always Free A1 settings must be <= 2 OCPUs and <= 12 GB RAM.")

# Reformat a flattened private key, while preserving already-correct PEM keys.
if "-----BEGIN" in private_key and "\n" not in private_key:
    private_key = private_key.replace("-----BEGIN PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----\n")
    private_key = private_key.replace("-----END PRIVATE KEY-----", "\n-----END PRIVATE KEY-----")

config = {
    "user": user_id,
    "fingerprint": fingerprint,
    "tenancy": tenancy_id,
    "region": region,
    "key_content": private_key,
}

try:
    compute = oci.core.ComputeClient(config)
    identity = oci.identity.IdentityClient(config)
    region_subscriptions = identity.list_region_subscriptions(tenancy_id).data
    home_region = next(
        (item.region_name for item in region_subscriptions if item.is_home_region),
        None,
    )
    if not home_region:
        sys.exit("Could not determine the tenancy home region; refusing to launch.")
    if region.lower() != home_region.lower():
        sys.exit(
            f"Refusing to launch: OCI_REGION={region!r} is not the tenancy home "
            f"region ({home_region!r}); A1 Always Free applies in the home region."
        )
    ads = [ad.name for ad in identity.list_availability_domains(tenancy_id).data]
    if not ads:
        raise RuntimeError("OCI returned no availability domains.")
except Exception as exc:
    sys.exit(f"OCI initialization failed; check credentials and region: {exc}")

if not image_id:
    try:
        images = compute.list_images(
            compartment_id=tenancy_id,
            operating_system="Canonical Ubuntu",
            shape=SHAPE,
            sort_by="TIMECREATED",
            sort_order="DESC",
        ).data
        if not images:
            sys.exit("No Canonical Ubuntu A1 image found in this region.")
        image_id = images[0].id
        print(f"Using latest Canonical Ubuntu image: {image_id}", flush=True)
    except oci.exceptions.ServiceError as exc:
        sys.exit(f"Could not find Ubuntu image. Check OCI_IMAGE_ID: {exc}")


def existing_instance():
    # Prevent later scheduled runs from creating duplicate VMs.
    page = None
    while True:
        response = compute.list_instances(
            compartment_id=tenancy_id,
            page=page,
        )
        for instance in response.data:
            if (instance.display_name == DISPLAY_NAME
                    and instance.lifecycle_state != "TERMINATED"):
                return instance
        page = response.headers.get("opc-next-page")
        if not page:
            return None


def retry_delay(error, failure_count):
    message = str(getattr(error, "message", error)).lower()
    status = getattr(error, "status", None)
    capacity = status == 500 or any(word in message for word in (
        "out of host capacity", "out of capacity"
    ))
    if capacity:
        return 60
    if status == 429:
        return min(60 * max(failure_count, 1), 300)
    if status is not None and 500 <= status < 600:
        return min(30 * max(failure_count, 1), 300)
    # Permanent client errors (bad subnet, image, quota, permission) should
    # stop instead of hammering OCI forever.
    if status is not None and 400 <= status < 500:
        sys.exit(f"Permanent OCI error ({status}); fix configuration/quota: {error}")
    return min(30 * max(failure_count, 1), 300)


print(
    f"Retrying A1 launch until an instance exists: {region}, {ocpus:g} OCPU, "
    f"{memory_gbs:g} GB RAM. Stop with Ctrl+C.",
    flush=True,
)
failures = 0
while True:
    try:
        found = existing_instance()
        if found:
            print(
                f"Instance already exists: {found.id} ({found.lifecycle_state}); "
                "not creating another.",
                flush=True,
            )
            break

        for ad in ads:
            # Re-check between ADs so a second concurrent run cannot create
            # another instance after the first request has been accepted.
            found = existing_instance()
            if found:
                print(f"Instance now exists: {found.id} ({found.lifecycle_state}).", flush=True)
                break
            print(f"{datetime.now(timezone.utc).isoformat()} Trying {ad}...", flush=True)
            details = oci.core.models.LaunchInstanceDetails(
                compartment_id=tenancy_id,
                availability_domain=ad,
                shape=SHAPE,
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=ocpus,
                    memory_in_gbs=memory_gbs,
                ),
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    source_type="image",
                    image_id=image_id,
                ),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=subnet_id,
                    assign_public_ip=True,
                ),
                metadata={"ssh_authorized_keys": public_ssh_key},
                display_name=DISPLAY_NAME,
            )
            try:
                result = compute.launch_instance(details)
                print(f"Launch accepted. Instance ID: {result.data.id}", flush=True)
                sys.exit(0)
            except oci.exceptions.ServiceError as exc:
                failures += 1
                delay = retry_delay(exc, failures)
                print(f"OCI error; retrying in {delay}s: {exc}", flush=True)
                time.sleep(delay)
        else:
            # All ADs were tried without capacity. Wait before another full pass.
            time.sleep(60)
            continue
        if found:
            break
    except KeyboardInterrupt:
        print("Stopped by user.", flush=True)
        sys.exit(130)
    except oci.exceptions.ServiceError as exc:
        failures += 1
        delay = retry_delay(exc, failures)
        print(f"OCI error; retrying in {delay}s: {exc}", flush=True)
        time.sleep(delay)
    except Exception as exc:
        failures += 1
        delay = min(30 * failures, 300)
        print(f"Unexpected error; retrying in {delay}s: {exc}", flush=True)
        time.sleep(delay)
