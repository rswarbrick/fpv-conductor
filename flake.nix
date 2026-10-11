# Copyright lowRISC contributors.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
{
  description = "Flake for building fpv-conductor tool";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    pyproject-nix.url = "github:pyproject-nix/pyproject.nix";
    pyproject-nix.inputs.nixpkgs.follows = "nixpkgs";

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    fenix = {
      url = "github:nix-community/fenix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { self,
      nixpkgs,
      flake-utils,
      uv2nix,
      pyproject-nix,
      pyproject-build-systems,
      fenix,
      ... }:
    flake-utils.lib.eachDefaultSystem (system: let
      workspace = uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ./.;};
      pkgs = nixpkgs.legacyPackages.${system};
      python = pkgs.python313;
      inherit (pkgs.callPackages pyproject-nix.build.util { }) mkApplication;

      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      pythonSet = (
        pkgs.callPackage pyproject-nix.build.packages {
          inherit python;
        }
      ).overrideScope (
        nixpkgs.lib.composeManyExtensions [
          pyproject-build-systems.overlays.default
          overlay
        ]
      );

      virtualenv = pythonSet.mkVirtualEnv "fpv-conductor-dev-env" workspace.deps.default;

      # rIC3 needs a nightly Rust toolchain because it depends on packages that use the
      # #![feature...] syntax. This isn't supported by stable Rust.
      toolchain = fenix.packages.${system}.toolchainOf {
        channel = "nightly";
        date = "2025-07-29";
        sha256 = "sha256-6D2b7glWC3jpbIGCq6Ta59lGCKN9sTexhgixH4Y7Nng=";
      };
      rustPlatform = pkgs.makeRustPlatform {
        inherit (toolchain) rustc cargo;
      };
      ric3_src = pkgs.fetchCrate {
        pname = "rIC3";
        version = "1.4.1";
        sha256 = "0713ncxbnz7phcnlcb5sgrwcjf3a8iapl027lca4g0aacybsgxsq";
      };
      ric3 = rustPlatform.buildRustPackage {
        pname = "ric3";
        version = "1.4.1";
        cargoLock = {
          lockFile = "${ric3_src}/Cargo.lock";
        };
        nativeBuildInputs = with pkgs; [
          cmake
          clang
          # This ensures that the built binary gets patched so that it points properly at libraries
          # on which it depends. If you don't do this, a devshell using the package needs to hack
          # LD_LIBRARY_PATH explicitly.
          autoPatchelfHook
        ];
        # This build input is needed to ensure that autoPatchelfHook works properly
        buildInputs = [ pkgs.stdenv.cc.cc.lib ];
        src = ric3_src;
      };
    in {
      packages.default =
        let
          app = mkApplication {
            venv = virtualenv;
            package = pythonSet.fpv-conductor;
          };
        in
          pkgs.symlinkJoin {
            name = "fpv-conductor-with-deps";
            paths = [ app ];
            buildInputs = [ pkgs.makeWrapper ];
            postBuild = ''
              wrapProgram $out/bin/fpv-conductor \
                --prefix PATH : ${pkgs.lib.makeBinPath [ ric3 ]}
            '';
          };

      apps.default = {
        type = "app";
        program = "${self.packages.${system}.default}/bin/fpv-conductor";
      };

      devShells.default = pkgs.mkShell {
        packages = [
          virtualenv
          pkgs.uv

          ric3
        ];

        env = {
          UV_NO_SYNC = "1";
          UV_PYTHON_DOWNLOADS = "never";
          UV_PYTHON = python.interpreter;
        };

        shellHook = ''
          unset PYTHONPATH
          export REPO_ROOT=$(git rev-parse --show-toplevel)
        '';
      };
    });
}
