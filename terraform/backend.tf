terraform {
  backend "azurerm" {
    resource_group_name  = "Tempest"
    storage_account_name = "tempest31"
    container_name       = "tfstate"
    key                  = "prod.terraform.tfstate"
  }
}