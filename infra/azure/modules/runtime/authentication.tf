# A delegated API scope keeps ARM tokens and app-only worker tokens out of the
# user admission path. The CLI client is pre-authorized for the synthetic pilot.
resource "azuread_application" "api" {
  # The identifier URI uses the generated client ID and is owned by the
  # separate provider resource below. Do not clear it on the next root apply.
  lifecycle {
    ignore_changes = [identifier_uris]
  }
  owners           = [var.operator_id]
  display_name     = "${var.prefix}-api"
  sign_in_audience = "AzureADMyOrg"
  api {
    requested_access_token_version = 2
    oauth2_permission_scope {
      id                         = "6371ea8e-8b41-44d6-8488-c8a417caa190"
      value                      = "runtime.invoke"
      type                       = "User"
      admin_consent_display_name = "Invoke the synthetic BankerIQ pilot"
      admin_consent_description  = "Accept and read the permitted synthetic runtime input."
      user_consent_display_name  = "Invoke the synthetic BankerIQ pilot"
      user_consent_description   = "Accept and read the permitted synthetic runtime input."
    }
  }
}

resource "azuread_application_identifier_uri" "api" {
  application_id = azuread_application.api.id
  identifier_uri = "api://${azuread_application.api.client_id}"
}

resource "azuread_service_principal" "api" {
  owners    = [var.operator_id]
  client_id = azuread_application.api.client_id
}

resource "azuread_application_pre_authorized" "cli" {
  application_id       = azuread_application.api.id
  authorized_client_id = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
  permission_ids       = ["6371ea8e-8b41-44d6-8488-c8a417caa190"]
}
