#!/bin/sh
# nginx entrypoint — substitutes env vars into the template then starts nginx.
set -e

# Build resolver directive from environment variables (with defaults)
DNS_PRIMARY="${DNS_PRIMARY:-8.8.8.8}"
DNS_FALLBACK="${DNS_FALLBACK:-1.1.1.1}"
DNS_PRIMARY_V6="${DNS_PRIMARY_V6:-2001:4860:4860::8888}"
DNS_FALLBACK_V6="${DNS_FALLBACK_V6:-2606:4700:4700::1111}"

export DNS_RESOLVERS="resolver ${DNS_PRIMARY} ${DNS_FALLBACK} ${DNS_PRIMARY_V6} ${DNS_FALLBACK_V6} valid=300s ipv6=on;"

# Substitute the template
envsubst '${DNS_RESOLVERS}' < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf

# Start nginx with the generated config
exec nginx -g 'daemon off;'