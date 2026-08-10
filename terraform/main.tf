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
    site_config {}
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
