FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html   /usr/share/nginx/html/index.html
COPY logs.html    /usr/share/nginx/html/logs.html
COPY control.html /usr/share/nginx/html/control.html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
