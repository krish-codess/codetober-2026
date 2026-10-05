# syntax=docker/dockerfile:1
FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY cmd cmd
COPY internal internal
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/ ./cmd/...

# Runtime: two static binaries, the migration files, a shell for healthchecks. No compiler, no sources.
FROM alpine:3.22
RUN adduser -D -u 10001 ship && mkdir -p /data/raw && chown -R ship /data
COPY --from=build /out/shipd /out/shop /usr/local/bin/
COPY migrations /migrations
USER ship
EXPOSE 8080 9100
ENTRYPOINT ["shipd"]
CMD ["serve"]
