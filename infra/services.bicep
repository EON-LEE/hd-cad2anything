targetScope = 'resourceGroup'

@description('Environment name used for the base deployment; resource names are derived the same way as resources.bicep.')
param environmentName string
param location string = resourceGroup().location

@description('Container image for the Atlas service (web, REST/chat API, and MCP endpoint).')
param atlasImage string

@secure()
@description('Shared key that Foundry sends to the MCP endpoint through the project connection.')
param mcpApiKey string

param chatModelName string = 'gpt-6.1-sol'
param miniModelName string = 'gpt-5.4-mini'

var token = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
  project: 'cad2anything'
}

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-${token}'
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: 'cr${token}'
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: 'cae-${token}'
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: 'appi-${token}'
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: 'psql-${token}'
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: 'aif-${token}'
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundry
  name: 'cad2anything'
}

// One replica keeps the in-memory workspace consistent; Blob Storage and PostGIS hold durable state.
resource atlasApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-atlas-${token}'
  location: location
  tags: union(tags, { 'azd-service-name': 'atlas' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${appIdentity.id}': {} }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: [
        { name: 'mcp-api-key', value: mcpApiKey }
      ]
    }
    template: {
      containers: [
        {
          name: 'atlas'
          image: atlasImage
          resources: { cpu: json('2.0'), memory: '4Gi' }
          env: [
            { name: 'AZURE_CLIENT_ID', value: appIdentity.properties.clientId }
            { name: 'WORKSPACE_DIR', value: '/tmp/workspace' }
            { name: 'DATA_STORAGE_ACCOUNT', value: 'st${token}' }
            { name: 'DATA_CONTAINER', value: 'data' }
            { name: 'FOUNDRY_PROJECT_ENDPOINT', value: 'https://${foundry.name}.services.ai.azure.com/api/projects/${project.name}' }
            { name: 'CHAT_MODEL_DEPLOYMENT', value: chatModelName }
            { name: 'MINI_MODEL_DEPLOYMENT', value: miniModelName }
            { name: 'POSTGRES_HOST', value: postgres.properties.fullyQualifiedDomainName }
            { name: 'POSTGRES_DATABASE', value: 'cad2anything' }
            { name: 'POSTGRES_USER', value: appIdentity.name }
            { name: 'MCP_API_KEY', secretRef: 'mcp-api-key' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 8000 }
              periodSeconds: 15
            }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

// Foundry project connection that stores the MCP key; agents reference it by connection ID.
resource mcpConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: 'cad2anything-mcp'
  properties: {
    category: 'RemoteTool'
    target: 'https://${atlasApp.properties.configuration.ingress.fqdn}/mcp'
    authType: 'CustomKeys'
    isSharedToAll: true
    credentials: {
      keys: {
        'x-api-key': mcpApiKey
      }
    }
  }
}

output ATLAS_URL string = 'https://${atlasApp.properties.configuration.ingress.fqdn}'
output MCP_SERVER_URL string = 'https://${atlasApp.properties.configuration.ingress.fqdn}/mcp'
output MCP_CONNECTION_ID string = mcpConnection.id
output MCP_CONNECTION_NAME string = mcpConnection.name
