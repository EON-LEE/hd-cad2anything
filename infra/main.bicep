targetScope = 'subscription'

@minLength(1)
@maxLength(32)
@description('Environment name used to derive resource names, for example "cad2anything-dev".')
param environmentName string

@description('Azure region for all resources.')
param location string = 'koreacentral'

@description('Object ID of the developer principal that receives data-plane roles.')
param principalId string

@description('User principal name of the developer, used as a PostgreSQL Microsoft Entra administrator.')
param principalName string

@description('Optional client IP allowed through the PostgreSQL firewall for local data loading.')
param clientIpAddress string = ''

@description('Primary reasoning model deployment.')
param chatModelName string = 'gpt-6.1-sol'
param chatModelVersion string = '2026-09-29'
param chatModelCapacity int = 250

@description('Lightweight model deployment for simple steps.')
param miniModelName string = 'gpt-5.4-mini'
param miniModelVersion string = '2026-03-17'
param miniModelCapacity int = 150

@description('Embedding model deployment for file search.')
param embeddingModelName string = 'text-embedding-3-small'
param embeddingModelVersion string = '1'
param embeddingModelCapacity int = 50

var tags = {
  'azd-env-name': environmentName
  project: 'cad2anything'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module resources 'resources.bicep' = {
  name: 'resources'
  scope: rg
  params: {
    environmentName: environmentName
    location: location
    tags: tags
    principalId: principalId
    principalName: principalName
    clientIpAddress: clientIpAddress
    chatModelName: chatModelName
    chatModelVersion: chatModelVersion
    chatModelCapacity: chatModelCapacity
    miniModelName: miniModelName
    miniModelVersion: miniModelVersion
    miniModelCapacity: miniModelCapacity
    embeddingModelName: embeddingModelName
    embeddingModelVersion: embeddingModelVersion
    embeddingModelCapacity: embeddingModelCapacity
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_LOCATION string = location
output FOUNDRY_ACCOUNT_NAME string = resources.outputs.foundryAccountName
output FOUNDRY_PROJECT_NAME string = resources.outputs.foundryProjectName
output FOUNDRY_PROJECT_ENDPOINT string = resources.outputs.foundryProjectEndpoint
output AZURE_OPENAI_ENDPOINT string = resources.outputs.openAiEndpoint
output CHAT_MODEL_DEPLOYMENT string = chatModelName
output MINI_MODEL_DEPLOYMENT string = miniModelName
output EMBEDDING_MODEL_DEPLOYMENT string = embeddingModelName
output POSTGRES_HOST string = resources.outputs.postgresHost
output POSTGRES_DATABASE string = resources.outputs.postgresDatabase
output STORAGE_ACCOUNT_NAME string = resources.outputs.storageAccountName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.containerRegistryEndpoint
output AZURE_CONTAINER_APPS_ENVIRONMENT_NAME string = resources.outputs.containerAppsEnvironmentName
output AZURE_KEY_VAULT_NAME string = resources.outputs.keyVaultName
output APP_IDENTITY_CLIENT_ID string = resources.outputs.appIdentityClientId
output APP_IDENTITY_NAME string = resources.outputs.appIdentityName
output APPLICATIONINSIGHTS_CONNECTION_STRING string = resources.outputs.appInsightsConnectionString
