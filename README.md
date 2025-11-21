# jamftf-python-terraform-importer (Deployment Guide)

This document describes the _current, working_ way to set up and run this tool to generate Terraform import blocks from an existing Jamf Pro tenant.

---

## 1. What this tool does

- Connects to a Jamf Pro tenant via the Classic API (through `jamfpy`).
- Reads a JSON config (`jamftf.config.json`) describing which resource types to include.
- Fetches the selected resources (scripts, policies, configuration profiles, groups, etc.).
- Writes a Terraform `imports.hcl` file with `import { ... }` blocks you can feed into Terraform to import existing Jamf objects into state.

The entrypoint for day‑to‑day use is `jamftf_runner.py` in the project root.

---

## 2. Requirements

- **Python**: **3.10 or newer**
  - Both this project and the `jamfpy` dependency use the `X | Y` type‑hint syntax which requires Python ≥ 3.10.
- **Jamf Pro**
  - A Jamf Pro tenant with API access.
  - An OAuth2 API client (`client_id` / `client_secret`) with permissions to read the resources you want to import.
- **Terraform**
  - Terraform v1.5+ installed on your machine.

---

## 3. Clone and create a virtual environment

From a shell:

```bash
git clone https://github.com/deploymenttheory/jamftf-python-terraform-importer.git
cd jamftf-python-terraform-importer

# create a venv with Python 3.10+ (adjust python command as needed)
python3.12 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
```

If your system Python is already 3.10+, you can use `python3` instead of `python3.12`.

---

## 4. Install dependencies

`jamftf` depends on `jamfpy`, which is currently installed from GitHub.

Inside the activated virtual environment, from the project root:

```bash
# install jamfpy (Jamf API client)
pip install "git+https://github.com/thejoeker12/jamfpy"

# install this project (editable is convenient while you’re working from source)
pip install -e .
```

If you prefer a non‑editable install, use:

```bash
pip install .
```

> **Note:** If you see errors mentioning `unsupported operand type(s) for |: 'type' and 'NoneType'`, you are almost certainly running Python < 3.10 in your virtual environment. Re‑create the venv with Python 3.10+ and reinstall.

---

## 5. Configure Jamf credentials

`jamftf_runner.py` reads Jamf connection details from environment variables:

```bash
export JAMF_URL="https://<tenant>.jamfcloud.com"
export JAMF_CLIENT_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
export JAMF_CLIENT_SECRET="********************************"
```

Notes:

- `JAMF_URL` can be a full URL (`https://tenant.jamfcloud.com`) or just the host (`tenant.jamfcloud.com`). The runner will try both.
- Do **not** hard‑code credentials into files committed to version control; prefer environment variables or secrets managers.

---

## 6. Configure which resources to import

The runner expects a JSON config that maps **provider resource tags** to booleans. A value of `true` enables that resource type.

The supported keys (from `jamftf.enums.ProviderResourceTags`) are:

- `jamfpro_script`
- `jamfpro_category`
- `jamfpro_policy`
- `jamfpro_macos_configuration_profile_plist`
- `jamfpro_static_computer_group`
- `jamfpro_smart_computer_group`
- `jamfpro_advanced_computer_search`
- `jamfpro_computer_extension_attribute`

An example `jamftf.config.json` (checked into this repo) looks like:

```json
{
  "jamfpro_script": false,
  "jamfpro_category": false,
  "jamfpro_policy": true,
  "jamfpro_macos_configuration_profile_plist": false,
  "jamfpro_static_computer_group": false,
  "jamfpro_smart_computer_group": false,
  "jamfpro_advanced_computer_search": false,
  "jamfpro_computer_extension_attribute": false
}
```

Adjust these booleans according to which resource types you want imported.

---

## 7. Run the importer

With the venv active, environment variables set, and `jamftf.config.json` in place, run:

```bash
python jamftf_runner.py \
  --config jamftf.config.json \
  --out imports.hcl \
  --dump item_dump.jsonl
```

Flags:

- `--config` (required): Path to the JSON config file (e.g. `jamftf.config.json`).
- `--out` (optional): Output HCL file path. Defaults to `imports.hcl`.
- `--dump` (optional): If provided, a small sample of raw API items (per resource) is written as JSONL to this path for debugging (for example `item_dump.jsonl`).

On success, you should see output similar to:

```text
[runner] Tenant OK with fqdn='...', auth_method='oauth2'
Wrote imports.hcl with <N> lines.
```

---

## 8. Using the generated `imports.hcl` with Terraform

The generated `imports.hcl` contains blocks of the form:

```hcl
import {
  id = "123"
  to = jamfpro_policy.some_policy_name
}
```

Typical workflow:

1. Add matching resource definitions to your Terraform configuration (e.g. `jamfpro_policy.some_policy_name`).
2. Run Terraform with the import file:

   ```bash
   terraform init
   terraform plan
   terraform apply -refresh-only   # optional sanity check
   terraform apply -var-file=...   # as per your workflow
   ```

3. Use `terraform show` or `terraform state list` to confirm that the Jamf resources are now in state.

The exact Jamf provider configuration is outside the scope of this repo; configure it according to the provider’s documentation.

---

## 9. Troubleshooting

- **`Failed to import jamfpy. Install with ... Error: unsupported operand type(s) for |: 'type' and 'NoneType'`**
  - Your interpreter is almost certainly Python 3.8/3.9 inside the venv.
  - Fix: remove/recreate the venv with Python 3.10+ and reinstall dependencies.

- **SSL / `NotOpenSSLWarning` from `urllib3`**
  - On some macOS setups you may see warnings about LibreSSL vs OpenSSL when importing `requests`/`urllib3`.
  - These are usually warnings only; if you see connection failures, ensure your system OpenSSL/CA certificates are up to date, or use a Python build linked against a modern OpenSSL.

If you run into issues not covered here, capture the full traceback and commands you ran and open an issue on the GitHub repository.

