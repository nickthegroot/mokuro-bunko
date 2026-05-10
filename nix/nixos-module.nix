{
  config,
  lib,
  pkgs,
  ...
}:

with lib;

let
  cfg = config.services.mokuro-bunko;
  yamlFormat = pkgs.formats.yaml { };
  basePath = cfg.settings.storage.base_path;
in
{
  options.services.mokuro-bunko = {
    enable = mkEnableOption "mokuro-bunko server";

    package = mkOption {
      type = types.package;
      default = pkgs.mokuro-bunko;
      description = "The mokuro-bunko package to use.";
    };

    user = mkOption {
      type = types.str;
      default = "mokuro-bunko";
      description = "User to run the mokuro-bunko service as.";
    };

    group = mkOption {
      type = types.str;
      default = "mokuro-bunko";
      description = "Group to run the mokuro-bunko service as.";
    };

    data_path = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = ''
        Optional path to the library data directory. When set, a symlink is
        created at ''${storage.base_path}/library pointing to
        this path, and the service is granted read/write access to it regardless
        of where it lives on the filesystem.
      '';
      example = "/mnt/nas/manga";
    };

    settings = mkOption {
      inherit (yamlFormat) type;
      default = { };
      description = ''
        Mokuro Bunko configuration. See the upstream documentation for available options.
      '';
      example = {
        server = {
          host = "0.0.0.0";
          port = 8080;
        };
        storage.base_path = "/var/lib/mokuro-bunko";
        registration.mode = "self";
      };
    };
  };

  config = mkIf cfg.enable {
    # Default settings merged with user settings
    services.mokuro-bunko.settings = {
      server = mkDefault {
        host = "0.0.0.0";
        port = 8080;
      };
      storage = mkDefault {
        base_path = "/var/lib/mokuro-bunko";
      };
      registration = mkDefault {
        mode = "self";
        default_role = "registered";
        allow_anonymous_browse = true;
        allow_anonymous_download = true;
      };
      cors = mkDefault {
        enabled = true;
        allowed_origins = [
          "https://reader.mokuro.app"
          "http://localhost:5173"
          "http://localhost:*"
          "http://127.0.0.1:*"
        ];
        allow_credentials = true;
      };
      ssl = mkDefault {
        enabled = false;
        auto_cert = false;
      };
      admin = mkDefault {
        enabled = true;
        path = "/_admin";
      };
      catalog = mkDefault {
        enabled = false;
        reader_url = "https://reader.mokuro.app";
        use_as_homepage = false;
      };
      queue = mkDefault {
        show_in_nav = false;
        public_access = true;
      };
      ocr = mkDefault {
        backend = "auto";
        poll_interval = 30;
      };
    };

    # Ensure the base directory exists with correct ownership before the service starts.
    systemd.tmpfiles.rules =
      [
        "d ${basePath} 0770 ${cfg.user} ${cfg.group} -"
      ]
      ++ optionals (cfg.data_path != null) [
        # Symlink <basePath>/library → data_path
        "L+ ${basePath}/library - - - - ${cfg.data_path}"
      ];

    systemd.services.mokuro-bunko = {
      description = "Mokuro Bunko Server";
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        ExecStart = "${cfg.package}/bin/mokuro-bunko --config ${yamlFormat.generate "mokuro-bunko-config.yaml" cfg.settings} serve";
        Restart = "always";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = basePath;
        CapabilityBoundingSet = "";
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ReadOnlyPaths = [ "/" ];
        ReadWritePaths = [ basePath ] ++ optionals (cfg.data_path != null) [ cfg.data_path ];
        Environment = [
          "MOKURO_BUNKO_OCR_ENV=${pkgs.callPackage ./ocr-env.nix { }}"
        ];
      } // optionalAttrs (cfg.data_path != null) {
        # Make the external data_path visible inside the service's filesystem view.
        BindPaths = [ cfg.data_path ];
      };
    };

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = basePath;
    };

    users.groups.${cfg.group} = { };
  };
}
