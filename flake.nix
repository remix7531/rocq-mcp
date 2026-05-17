{
  description = "rocq-mcp — MCP server for Rocq/Coq proof development";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    # NOTE: pinned to the `feat/nix-flake` branch on the remix7531 fork while
    # the pytanque flake is in review upstream. Swap to
    # `github:LLM4Rocq/pytanque/<tag>` once the flake lands there.
    pytanque.url = "github:remix7531/pytanque/feat/nix-flake";
    pytanque.inputs.nixpkgs.follows = "nixpkgs";
    pytanque.inputs.flake-utils.follows = "flake-utils";
  };

  outputs = {
    self,
    nixpkgs,
    flake-utils,
    pytanque,
  }:
    flake-utils.lib.eachDefaultSystem (system: let
      pkgs = import nixpkgs {inherit system;};
      lib = pkgs.lib;
      pyproject = lib.importTOML ./pyproject.toml;
      version = pyproject.project.version;
      python = pkgs.python3;
      pytanquePkg = pytanque.packages.${system}.pytanque;

      rocq-mcp = python.pkgs.buildPythonPackage {
        pname = "rocq-mcp";
        inherit version;
        pyproject = true;

        src = ./.;

        build-system = [python.pkgs.setuptools];

        dependencies = [
          python.pkgs.fastmcp
          python.pkgs.psutil
          pytanquePkg
        ];

        pythonImportsCheck = ["rocq_mcp"];

        doCheck = false;

        meta.description = "MCP server for Rocq/Coq proof development";
      };
    in {
      packages = {
        inherit rocq-mcp;
        default = rocq-mcp;
      };

      apps.default = {
        type = "app";
        program = "${rocq-mcp}/bin/rocq-mcp";
      };

      devShells.default = pkgs.mkShell {
        packages = [
          (python.withPackages (ps:
            with ps; [
              fastmcp
              psutil
              pytest
              pytest-asyncio
              black
            ]))
          pytanquePkg
        ];

        shellHook = ''
          echo "rocq-mcp dev shell"
          echo "  run tests:   pytest"
          echo "  run server:  python -m rocq_mcp.server"
        '';
      };
    })
    // {
      overlays.default = final: prev: let
        pyproject = prev.lib.importTOML ./pyproject.toml;
        version = pyproject.project.version;
      in {
        python3 = (prev.extend pytanque.overlays.default).python3.override (old: {
          packageOverrides = prev.lib.composeExtensions (old.packageOverrides or (_: _: {})) (pyfinal: pyprev: {
            rocq-mcp = pyfinal.callPackage ({
              buildPythonPackage,
              setuptools,
              fastmcp,
              psutil,
              pytanque,
            }:
              buildPythonPackage {
                pname = "rocq-mcp";
                inherit version;
                pyproject = true;
                src = ./.;
                build-system = [setuptools];
                dependencies = [fastmcp psutil pytanque];
                pythonImportsCheck = ["rocq_mcp"];
                doCheck = false;
                meta.description = "MCP server for Rocq/Coq proof development";
              }) {};
          });
        });
      };
    };
}
