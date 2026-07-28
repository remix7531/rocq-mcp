{
  description = "MCP server for Rocq/Coq proof development";

  inputs = {
    # nixpkgs PR #542647: coq-lsp pet-server fix, pytanque, and rocq-mcp.
    nixpkgs.url = "github:NixOS/nixpkgs/pull/542647/head";
    flake-parts.url = "github:hercules-ci/flake-parts";
    flake-parts.inputs.nixpkgs-lib.follows = "nixpkgs";
  };

  outputs =
    inputs@{
      self,
      nixpkgs,
      flake-parts,
    }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = nixpkgs.lib.systems.flakeExposed;

      perSystem = { pkgs, self', ... }: {
        # The PR's rocq-mcp built from this checkout; building it runs the full test suite.
        packages.default = pkgs.rocq-mcp.overrideAttrs {
          version = (pkgs.lib.importTOML ./pyproject.toml).project.version;
          src = self;
        };

        checks.default = self'.packages.default;

        # inputsFrom provides the whole test toolchain, including coqc, pet, and the stdlib.
        devShells.default = pkgs.mkShell {
          inputsFrom = [ self'.packages.default ];

          # src layout: make `import rocq_mcp` work without installing.
          shellHook = ''
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
          '';
        };
      };
    };
}
