terraform {
    required_providers {
        azurerm = {
            source = "hashicorp/azurerm"
            version = "~> 3.0"
        }
    }
}

provider "azurerm" {
    features {}
}

resource "azurerm_linux_web_app" "webapp"{
    name = "Tempest34"
    location = data.azurerm_resource_group.rg.location
    service_plan_id = azurerm_service_plan.plan.id
    resource_group_name = data.azurerm_resource_group.rg.name

    site_config {
        # An empty site_config block doesn't mean "leave this alone" --
        # azurerm_linux_web_app manages app_command_line as an attribute
        # with an empty-string default, so omitting it here doesn't skip
        # it, it actively resets it to "" on every apply. That's what
        # wiped a manually-set startup command mid-deploy and left the
        # site serving Oryx's placeholder app instead of the real one.
        # Declaring it here makes Terraform's applied state match what
        # the app actually needs, so `terraform apply` stops undoing it.
        #
        # No application_stack block here on purpose: the pinned azurerm
        # provider (~> 3.0, currently 3.117.1) only validates
        # python_version up to "3.12" and doesn't yet know about 3.14,
        # even though Azure itself runs PYTHON|3.14 fine (confirmed
        # live) and this attribute wasn't what actually broke. Adding it
        # would fail `terraform plan` outright. Revisit once the
        # provider adds 3.14 to its accepted values, or bump the
        # provider version deliberately (bigger change, not this fix).
        app_command_line = "uvicorn app.main:app --host 0.0.0.0 --port 8000"
    }

    # Same drift-reset problem as app_command_line: leaving app_settings
    # undeclared doesn't mean "don't manage it", it means Terraform wants
    # to null out anything set outside of it -- confirmed live via
    # `terraform plan`, which showed both of these going to `-> null`
    # even though the CI deploy job re-sets them via `az CLI` right after
    # every apply. That ordering was accidentally masking this same bug;
    # declaring them here removes the dependency on job order entirely.
    app_settings = {
        SCM_DO_BUILD_DURING_DEPLOYMENT = "true"
        DATABASE_URL                   = "sqlite:////home/data/directory.db"
    }
}

resource "azurerm_service_plan" "plan" {
    name = "tempest-plan"
    resource_group_name = data.azurerm_resource_group.rg.name
    location =  data.azurerm_resource_group.rg.location
    os_type = "Linux"
    sku_name = "B1"

}
resource "azurerm_mssql_database" "database" {
    name = "tempest-database1"
    server_id = azurerm_mssql_server.server.id
    license_type = "LicenseIncluded"
    sku_name = "Basic"
}
resource "azurerm_mssql_server" "server" {
    name = "tempest-azure-sql"
    version = "12.0"
    resource_group_name = data.azurerm_resource_group.rg.name
    location = "West US 2"
    administrator_login = "QuadrantAdmin"
    administrator_login_password = var.db_pwd

    lifecycle {
      prevent_destroy = true
    }
}
resource "azurerm_storage_account" "sa"{
    name = "tempest31"
    resource_group_name = data.azurerm_resource_group.rg.name
    location = data.azurerm_resource_group.rg.location
    account_replication_type = "LRS"
    account_tier = "Standard"
}
resource "azurerm_storage_container" "sc" {
    name = "tfstate"
    storage_account_name = azurerm_storage_account.sa.name
}
