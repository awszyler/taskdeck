# taskdeck-sandbox-static
#
# Lightweight nginx serving /workspace at :8080. Used for static HTML
# demos (vanilla JS games, plain index.html, etc.) — anything that
# doesn't need a build step or runtime server.
#
# The user's workspace is bind-mounted at /workspace and served as-is.
FROM nginx:alpine

# gVisor's overlay returns ENOTSUP for some file operations on the
# default tmpfs/overlay layout. Two needed adjustments at build time:
#
# 1. Pre-create cache subdirs (nginx tries `mkdir` at startup which
#    fails on gVisor — ENOTSUP errno 95).
#
# 2. Move pid file to a writable location and disable daemon mode
#    in the main config so nginx doesn't try to open /run/nginx.pid
#    (which is also ENOTSUP under gVisor's tmpfs).
RUN mkdir -p /var/cache/nginx/client_temp \
             /var/cache/nginx/proxy_temp \
             /var/cache/nginx/fastcgi_temp \
             /var/cache/nginx/uwsgi_temp \
             /var/cache/nginx/scgi_temp \
    && chown -R nginx:nginx /var/cache/nginx \
    && chmod -R 700 /var/cache/nginx \
    # Use /tmp for pid (gVisor allows writes there); foreground mode.
    && sed -i 's|^pid .*$|pid /tmp/nginx.pid;|' /etc/nginx/nginx.conf \
    # Comment out the user directive — running as root in the
    # container is fine for our sandbox use, and avoids gVisor's
    # setuid quirks with the `nginx` user.
    && sed -i 's|^user .*$|# user nginx;|' /etc/nginx/nginx.conf

# nginx config:
# - listen 8080 (matches Detection.port for static)
# - root /workspace
# - if index.html exists, serve it at /
# - else show an autoindex listing so the user can click the file
#   the agent actually wrote (counter.html, demo.html, etc.)
# - SPA fallback to index.html when present
RUN cat > /etc/nginx/conf.d/default.conf <<'EOF'
server {
    listen 8080 default_server;
    server_name _;

    root /workspace;
    index index.html index.htm;

    # /* try the literal path → directory → SPA fallback to /index.html.
    # If /index.html doesn't exist either, we 404. The autoindex
    # directive below lets / show a directory listing in that case
    # so the user can click counter.html, demo.html, etc.
    location / {
        autoindex on;
        autoindex_exact_size off;
        try_files $uri $uri/ /index.html @autoindex;
    }

    # Fallback to a directory listing when no index file resolves.
    location @autoindex {
        autoindex on;
        try_files $uri/ =404;
    }

    # No caching during sandbox — agents iterate fast.
    expires off;
    add_header Cache-Control "no-store, no-cache, must-revalidate";
}
EOF

EXPOSE 8080
# nginx with daemon off so it stays in the foreground for docker.
CMD ["nginx", "-g", "daemon off;"]
