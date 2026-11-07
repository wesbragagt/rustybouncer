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
          version = "0.1.0";
          src = pkgs.lib.cleanSourceWith {
            src = ./.;
            filter =
              path: type:
              pkgs.lib.cleanSourceFilter path type
              && !(builtins.elem (builtins.baseNameOf path) [
                "target"
                ".direnv"
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
          ];
          doCheck = true;
          checkPhase = ''
            runHook preCheck
            bash scripts/verify
            runHook postCheck
          '';
        };

        checks.verification = self.packages.${system}.default;
        formatter = pkgs.nixfmt;

        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
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
          ];
          shellHook = ''
            bash scripts/install-hooks
          '';
        };
      }
    );
}
