# The coding sandbox: the environment in which the coding agent develops and tests the app.
#
# The container has internet access and the repository's working tree at /workspace. It has no
# email data, no configuration, and no credentials other than the coding agent's own login.
# The coding agent may change this file to add tools it needs; the image is rebuilt when the
# coding sandbox image is removed and `triage up` is run again.
#
# Tools are installed outside of /home/coder, because that directory is a volume which keeps
# the agent's login and caches across rebuilds.

FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl dovecot-imapd git less musl-tools \
        openssh-client pkg-config procps python3 ripgrep sudo unzip \
    && rm -rf /var/lib/apt/lists/*

# Rust, for the trusted base (the gateway and the host tool).
ENV RUSTUP_HOME=/opt/rust/rustup CARGO_HOME=/opt/rust/cargo
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --profile minimal --component clippy,rustfmt --no-modify-path \
    && /opt/rust/cargo/bin/rustup target add aarch64-apple-darwin \
    && chmod -R a+rwX /opt/rust

# uv, for the app (Python).
RUN curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

# The coding agent.
RUN npm install --global @anthropic-ai/claude-code && npm cache clean --force

RUN useradd --create-home --shell /bin/bash coder \
    && echo 'coder ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/coder \
    && git config --system user.name 'Email Triage coding agent' \
    && git config --system user.email 'coding-agent@localhost' \
    && git config --system --add safe.directory /workspace

USER coder
ENV PATH=/opt/rust/cargo/bin:/home/coder/.local/bin:$PATH \
    UV_LINK_MODE=copy \
    DISABLE_AUTOUPDATER=1
WORKDIR /workspace
CMD ["sleep", "infinity"]
