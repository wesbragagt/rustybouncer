{
  description = "rustybouncer development and verification";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python3.withPackages (ps: [ ps.psycopg ]);
      in
      {
        packages.default = pkgs.rustPlatform.buildRustPackage {
          pname = "rustybouncer";
          version = "1.0.1";
          src = pkgs.lib.cleanSourceWith {
            src = ./.;
            filter =
              path: type:
              pkgs.lib.cleanSourceFilter path type
              && !(builtins.elem (builtins.baseNameOf path) [
                "target"
                ".direnv"
                "metrics-evidence"
                "metrics-benchmark-evidence"
              ])
              && !(pkgs.lib.hasInfix "/benchmarks/runs" path);
          };
          cargoLock.lockFile = ./Cargo.lock;
          nativeBuildInputs = with pkgs; [
            actionlint
            ty
            rust-code-analysis
            clippy
            rustfmt
            python
            ruff
            nixfmt
            shellcheck
            prometheus.cli
          ];
          doCheck = true;
          checkPhase = ''
            runHook preCheck
            bash scripts/verify
            runHook postCheck
          '';
        };

        # Pure Nix image assembly: no Docker daemon, distribution, shell or
        # package manager. Include only the executable's runtime closure and NSS.
        packages.docker = pkgs.dockerTools.buildLayeredImage {
          name = "rustybouncer";
          tag = "latest";
          contents = [
            (pkgs.writeTextDir "etc/passwd" "rustybouncer:x:10001:10001:rustybouncer:/:/sbin/nologin\n")
            (pkgs.writeTextDir "etc/group" "rustybouncer:x:10001:\n")
            (pkgs.writeTextDir "etc/nsswitch.conf" "hosts: files dns\npasswd: files\ngroup: files\n")
          ];
          config = {
            User = "10001:10001";
            Entrypoint = [ "${self.packages.${system}.default}/bin/rustybouncer" ];
            Cmd = [ "/etc/rustybouncer/rustybouncer.toml" ];
            WorkingDir = "/";
            ExposedPorts = {
              "6432/tcp" = { };
            };
            Env = [ "RUST_LOG=rustybouncer=info" ];
          };
        };

        checks.verification = self.packages.${system}.default;
        formatter = pkgs.nixfmt;

        devShells.default = pkgs.mkShell {
          buildInputs =
            with pkgs;
            [
              rustc
              cargo
              clippy
              rustfmt
              rust-analyzer
              python
              ruff
              git
              docker
              postgresql
              nixfmt
              shellcheck
              ty
              rust-code-analysis
              pgbouncer
              actionlint
              kind
              kubectl
              prometheus.cli
            ]
            ++ lib.optionals stdenv.hostPlatform.isLinux [
              perf
              heaptrack
              strace
              util-linux
              valgrind
              openssl
            ];
          shellHook = ''
            bash scripts/install-hooks
          '';
        };
      }
    );
}
