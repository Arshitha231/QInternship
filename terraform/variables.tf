variable db_pwd {
    type = string
    description = "Password used for sql database."
    sensitive = true
}

# Azure OpenAI (chat + embeddings) and Azure AI Search credentials -- the
# same GROUP3_4OPENAI* / GROUP3_4_TEXT_EMBEDDING_3_SMALL_* / AISEARCH_*
# repo secrets the golden-eval CI step already uses (see ci-cd.yml), now
# also wired into the deployed app's own app_settings below. Before this,
# these secrets only ever reached the golden-eval test step -- the live
# App Service had none of them set, so app.tool_calling._mode() always
# fell back to "mock" in production, silently, with no error.
variable chat_endpoint {
    type = string
    description = "Azure OpenAI endpoint for chat (gpt-5)."
    sensitive = true
}
variable chat_key {
    type = string
    description = "Azure OpenAI API key for chat (gpt-5)."
    sensitive = true
}
variable embedding_endpoint {
    type = string
    description = "Azure OpenAI endpoint for embeddings (text-embedding-3-small)."
    sensitive = true
}
variable embedding_key {
    type = string
    description = "Azure OpenAI API key for embeddings (text-embedding-3-small)."
    sensitive = true
}
variable search_endpoint {
    type = string
    description = "Azure AI Search endpoint."
    sensitive = true
}
variable search_key {
    type = string
    description = "Azure AI Search API key."
    sensitive = true
}