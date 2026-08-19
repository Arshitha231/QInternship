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
    location = local.resource_default_loc
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
        # Migrations run here, at app startup, NOT from the CI runner. The
        # only SQL firewall rule is AllowAzureServices (0.0.0.0), which is
        # what lets this web app reach the database at all -- a GitHub-hosted
        # runner is not dependably covered by it, so `alembic upgrade head`
        # as a pipeline step would be at the mercy of which IP the runner
        # got. The App Service is already inside that boundary.
        #
        # Chained with `&&` deliberately: if the migration fails, the app
        # does not start, the deploy job's /health poll fails, and the
        # workflow goes red. The alternative -- start anyway on a stale
        # schema -- gives a green deploy serving 500s on every profile page,
        # since /health only does SELECT 1 and would keep passing. Same
        # reasoning as the health-check poll itself: fail loudly rather than
        # leave a silent 503.
        #
        # Idempotent and fast when the database is already at head, which is
        # every deploy that doesn't add a migration.
        app_command_line = "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"
    }

    # Same drift-reset problem as app_command_line: leaving app_settings
    # undeclared doesn't mean "don't manage it", it means Terraform wants
    # to null out anything set outside of it -- confirmed live via
    # `terraform plan`, which showed both of these going to `-> null`
    # even though the CI deploy job re-sets them via `az CLI` right after
    # every apply. That ordering was accidentally masking this same bug;
    # declaring them here removes the dependency on job order entirely.
    #
    # DATABASE_URL now points at the real Azure SQL database
    # (tempest-database1) instead of the local sqlite file -- schema and
    # data were migrated and verified there already (see
    # alembic/versions/a3f0c9d2e1b4_*.py and the AllowAzureServices
    # firewall rule below). Built from the server/database resources'
    # own attributes rather than hardcoded strings so it can't drift if
    # either is ever renamed. var.db_pwd is marked sensitive, which is
    # why this whole value is redacted in `terraform plan`/`apply` output.
    app_settings = {
        SCM_DO_BUILD_DURING_DEPLOYMENT = "true"
        DATABASE_URL                   = "mssql+pymssql://${azurerm_mssql_server.server.administrator_login}:${var.db_pwd}@${azurerm_mssql_server.server.fully_qualified_domain_name}:1433/${azurerm_mssql_database.database.name}"

        # Real AI resolution instead of the silent mock fallback -- see
        # variables.tf's comment on why these were missing before.
        CHAT_ENDPOINT               = var.chat_endpoint
        CHAT_KEY                    = var.chat_key
        OPENAI_CHAT_DEPLOYMENT      = "gpt-5"
        EMBEDDING_ENDPOINT          = var.embedding_endpoint
        EMBEDDING_KEY               = var.embedding_key
        OPENAI_EMBEDDING_DEPLOYMENT = "text-embedding-3-small"
        SEARCH_ENDPOINT             = var.search_endpoint
        SEARCH_KEY                  = var.search_key

        # This deploy authenticates with app/auth.py's DEV provider: role
        # and identity come from a client-supplied X-Dev-Role header with no
        # credential check, and the demo login page (app/demo_auth.py) is
        # the sign-in flow. That has always been true here -- there is no
        # Entra app registration behind this site -- but it used to be true
        # by omission, which is exactly what
        # app/auth.py's assert_dev_auth_is_intentional() now refuses to
        # start on. Saying it out loud is the whole point of that guard.
        #
        # It belongs in THIS block and not only in the CI job's `az webapp
        # config appsettings set`, for the reason the comment above already
        # gives: an undeclared setting is one Terraform wants to null out,
        # so a value living only in the CI job would be wiped by the very
        # next apply -- which runs on the same push, just before it.
        #
        # Replace with ENTRA_TENANT_ID/ENTRA_CLIENT_ID to make this a real
        # auth deploy; auth_mode() switches on their presence, and this
        # setting then does nothing.
        ALLOW_DEV_AUTH = "1"
    }
}

resource "azurerm_service_plan" "plan" {
    name = "tempest-plan"
    resource_group_name = data.azurerm_resource_group.rg.name
    location =  local.resource_default_loc
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
    location = local.resource_default_loc
    administrator_login = "QuadrantAdmin"
    administrator_login_password = var.db_pwd

    lifecycle {
      prevent_destroy = true
    }
}

# Azure SQL blocks every connection by default. Start/end both 0.0.0.0 is
# the documented special case Azure recognizes as "allow traffic from any
# Azure resource" (App Service included) -- not a real IP range, and not
# open to the public internet.
resource "azurerm_mssql_firewall_rule" "allow_azure_services" {
    name             = "AllowAzureServices"
    server_id        = azurerm_mssql_server.server.id
    start_ip_address = "0.0.0.0"
    end_ip_address   = "0.0.0.0"
}
resource "azurerm_storage_account" "sa"{
    name = "tempest31"
    resource_group_name = data.azurerm_resource_group.rg.name
    location = local.resource_default_loc
    account_replication_type = "LRS"
    account_tier = "Standard"
}
resource "azurerm_storage_container" "sc" {
    name = "tfstate"
    storage_account_name = azurerm_storage_account.sa.name
}
