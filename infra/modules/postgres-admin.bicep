@description('Assigns a Microsoft Entra administrator to a PostgreSQL flexible server. A module is used so that runtime principal IDs can name the child resource.')
param serverName string
param principalId string
param principalName string

@allowed(['User', 'Group', 'ServicePrincipal'])
param principalType string

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: serverName
}

resource admin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: server
  name: principalId
  properties: {
    principalType: principalType
    principalName: principalName
    tenantId: subscription().tenantId
  }
}
