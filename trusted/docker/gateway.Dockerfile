# The gateway image. Part of the trusted base: build it from a fresh clone at a reviewed commit
# (see README.md), with the `trusted/` directory as build context:
#
#     docker build -f trusted/docker/gateway.Dockerfile -t triage-gateway trusted
#
# The binary is statically linked against musl and runs as an unprivileged user in an otherwise
# empty image: no shell, no package manager. TLS trust anchors are compiled into the binary.

FROM rust:1-alpine AS build
RUN apk add --no-cache musl-dev
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
# Cargo reads the manifest of every member of the workspace, also when only one of them is
# built, so the host tool's directory has to be here although none of it is compiled.
COPY gateway ./gateway
COPY host ./host
RUN cargo build --release --locked -p triage-gateway \
    && cp target/release/triage-gateway /triage-gateway

FROM scratch
COPY --from=build /triage-gateway /triage-gateway
USER 10001:10001
VOLUME /data
EXPOSE 8100 8101 8102
ENTRYPOINT ["/triage-gateway"]
