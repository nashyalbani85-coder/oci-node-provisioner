import os
import time
import oci

# ---------------------------------------------------------------------------
# 1. RETRIEVE CONFIGURATION FROM GITHUB SECRETS & VARIABLES
# ---------------------------------------------------------------------------
user_id = os.getenv("OCI_USER_ID")
fingerprint = os.getenv("OCI_FINGERPRINT")
tenancy_id = os.getenv("OCI_TENANCY_ID")
region = os.getenv("OCI_REGION", "ap-singapore-1")
private_key = os.getenv("OCI_PRIVATE_KEY")

subnet_id = os.getenv("OCI_SUBNET_ID")
image_id = os.getenv("OCI_IMAGE_ID")
public_ssh_key = os.getenv("OCI_PUBLIC_SSH_KEY")

# Retrieve instance specs from GitHub Variables (Default: 2 OCPU / 12 GB RAM)
try:
    ocpus = float(os.getenv("OCI_OCPUS", "2"))
    memory_in_gbs = float(os.getenv("OCI_MEMORY_IN_GBS", "12"))
except ValueError:
    ocpus = 2.0
    memory_in_gbs = 12.0

# Verify critical secrets are loaded
if not all([user_id, fingerprint, tenancy_id, private_key, subnet_id, image_id, public_ssh_key]):
    print("CRITICAL ERROR: One or more required OCI Secrets are missing from GitHub Repository Secrets!")
    exit(1)

# Format private key in case newlines were lost during upload
if "-----BEGIN" in private_key and "\n" not in private_key:
    private_key = private_key.replace("-----BEGIN PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----\n")
    private_key = private_key.replace("-----END PRIVATE KEY-----", "\n-----END PRIVATE KEY-----")

config = {
    "user": user_id,
    "fingerprint": fingerprint,
    "tenancy": tenancy_id,
    "region": region,
    "key_content": private_key
}

# ---------------------------------------------------------------------------
# 2. INITIALIZE OCI COMPUTE CLIENT
# ---------------------------------------------------------------------------
try:
    compute_client = oci.core.ComputeClient(config)
    identity_client = oci.identity.IdentityClient(config)
    print("OCI Authentication Successful. Initializing loop sequence...")
except Exception as e:
    print(f"Authentication Failed: {e}")
    exit(1)

# Dynamically fetch Availability Domains for the region or fallback to Singapore AD
try:
    ad_list = identity_client.list_availability_domains(tenancy_id).data
    ads = [ad.name for ad in ad_list]
    print(f"Found Availability Domains: {ads}")
except Exception as e:
    print(f"Warning: Could not fetch ADs dynamically ({e}). Falling back to default Singapore AD.")
    ads = [f"{tenancy_id}:AP-SINGAPORE-1-AD-1"]

# ---------------------------------------------------------------------------
# 3. PROVISIONING LOOP
# ---------------------------------------------------------------------------
max_attempts = 60
shape = "VM.Standard.A1.Flex"

print(f"Targeting Shape: {shape} ({ocpus} OCPU / {memory_in_gbs} GB RAM)")

for attempt in range(1, max_attempts + 1):
    for ad in ads:
        print(f"[{attempt}/{max_attempts}] Requesting instance in {ad}...")
        
        launch_details = oci.core.models.LaunchInstanceDetails(
            compartment_id=tenancy_id,
            availability_domain=ad,
            shape=shape,
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ocpus,
                memory_in_gbs=memory_in_gbs
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                source_type="image",
                image_id=image_id
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True
            ),
            metadata={
                "ssh_authorized_keys": public_ssh_key
            },
            display_name="Ampere-A1-Singapore"
        )

        try:
            response = compute_client.launch_instance(launch_details)
            print("SUCCESS! Instance created successfully!")
            print(f"Instance ID: {response.data.id}")
            exit(0)
        except oci.exceptions.ServiceError as e:
            if e.status == 500 or "Out of host capacity" in str(e.message) or "LimitExceeded" in str(e.message):
                print(f"-> Capacity/Limit Issue in {ad}: {e.message}")
            elif e.status == 401 or "Authentication" in str(e.message):
                print(f"-> API Error: {e.message}")
                print("Please verify OCI_PRIVATE_KEY, OCI_FINGERPRINT, and OCI_USER_ID in GitHub Secrets.")
                exit(1)
            else:
                print(f"-> Error ({e.status}): {e.message}")
        except Exception as e:
            print(f"-> Unexpected Error: {e}")

    time.sleep(10)

print("Loop finished without securing capacity. Will retry on next scheduled run.")
