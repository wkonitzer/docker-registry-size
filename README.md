# docker_registry_size
Tool to get the size of a MSR Registry

Requires
- MSR URL
- MSR Username
- Access token associated with the username

Can output a CSV file with the --csv option.

# Sample server implementation provided as well

Make sure to setup secrets
```
kubectl create secret generic msr-storage-metrics-credentials \
--from-literal=DOCKER_REGISTRY_USERNAME=myusername \
--from-literal=DOCKER_REGISTRY_TOKEN=mytoken \
-n msr-storage-metrics
```
