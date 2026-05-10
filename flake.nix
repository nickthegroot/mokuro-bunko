{
  description = "Mokuro Bunko - Self-hosted manga library server with WebDAV, OCR, and multi-user support";

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
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        packages = {
          mokuro-bunko = pkgs.callPackage ./nix/package.nix { };
          default = self.packages.${system}.mokuro-bunko;
        };

        devShells.default = import ./nix/shell.nix { inherit pkgs; };
      }
    )
    // {
      nixosModules = {
        mokuro-bunko = import ./nix/nixos-module.nix;
        default = self.nixosModules.mokuro-bunko;
      };
    };
}
