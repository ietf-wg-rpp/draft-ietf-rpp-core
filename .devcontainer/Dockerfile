FROM ghcr.io/ietf-tools/xml2rfc-base:latest

ARG USERNAME=vscode
ARG USER_UID=1000
ARG USER_GID=$USER_UID

# Install golang, mmark and the tools needed by the dev container
RUN apt-get -y update && apt-get -y install golang-go nano git sudo curl ca-certificates && apt-get -y clean && \
    git clone https://github.com/mmarkdown/mmark.git && \
    cd mmark && go get && go build && mv mmark /usr/bin/ && \
    cd .. && rm -rf mmark

# Create the non-root remote user (see "remoteUser" in devcontainer.json) with
# passwordless sudo, and pre-create the mount targets so they are owned by it
RUN groupadd --gid $USER_GID $USERNAME && \
    useradd --uid $USER_UID --gid $USER_GID -m -s /bin/bash $USERNAME && \
    echo "$USERNAME ALL=(root) NOPASSWD:ALL" > /etc/sudoers.d/$USERNAME && \
    chmod 0440 /etc/sudoers.d/$USERNAME && \
    mkdir -p /home/$USERNAME/.claude /home/$USERNAME/.config && \
    chown -R $USER_UID:$USER_GID /home/$USERNAME

ENV CLAUDE_CONFIG_DIR=/home/$USERNAME/.claude

USER $USERNAME

# Install the Claude Code CLI (native installer) for the remote user; it lands in
# ~/.local/bin, outside the ~/.claude volume mount
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH=/home/$USERNAME/.local/bin:$PATH
