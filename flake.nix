# =========================================================================
# restor8 — Juniper lab automation platform (single root flake + uv workspace)
# =========================================================================
# Deviation from the single-service template (decision in TODO.md, 2026-08-16):
# a per-service flake cannot reference ../../libs (flake evaluation is rooted
# at the flake directory), so sharing libs/restor8_core would force vendoring
# it into every service. Instead: ONE flake + ONE uv.lock at the root, with a
# per-service image (nix build .#connector), devshell, and manifests/<svc>/.
# Everything else follows the pipeline unchanged: uv2nix venvs (no
# requirements.txt, no withPackages drift), non-root images, :latest +
# imagePullPolicy: Always, git-index-visible sources (git add -A first!).
#
# Add a service:
#   1. services/<name>/pyproject.toml (virtual member, restor8-core workspace dep)
#   2. add it to `serviceApps` below
#   3. manifests/<name>/{deployment,service}.yaml (ns restor8, port 8080)
#   4. `uv lock` → git add -A → just deploy <name>
# =========================================================================
{
  description = "restor8 — configure/backup/restore/test Juniper lab gear";

  inputs = {
    nixpkgs.url = "nixpkgs/nixos-26.05";   # flake.lock freezes the exact rev

    # uv2nix: build Python venvs from uv.lock — the single source of truth
    # for both the dev shell and every image.
    pyproject-nix.url = "github:pyproject-nix/pyproject.nix";
    pyproject-nix.inputs.nixpkgs.follows = "nixpkgs";
    uv2nix.url = "github:pyproject-nix/uv2nix";
    uv2nix.inputs.pyproject-nix.follows = "pyproject-nix";
    uv2nix.inputs.nixpkgs.follows = "nixpkgs";
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, uv2nix, pyproject-nix, pyproject-build-systems }:
    let
      systems = [ "x86_64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;

      # ── SERVICE REGISTRY ──────────────────────────────────────────────
      # name → app dir. Everything (image, justfile targets, manifests)
      # keys off these names. Image/deploy names: restor8-<name>.
      serviceApps = {
        connector = ./services/connector/app;
        inventory = ./services/inventory/app;
        backup = ./services/backup/app;
        restore = ./services/restore/app;
        topology = ./services/topology/app;
      };

      # Per-system: pkgs + the uv2nix python set shared by every service.
      perSystem = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          lib = nixpkgs.lib;

          # Load the whole workspace (root + libs/* + services/*) from
          # uv.lock; wheels preferred, sdists fall back to PEP-517 builds.
          workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
          overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

          pythonSet = (pkgs.callPackage pyproject-nix.build.packages {
            python = pkgs.python3;
          }).overrideScope (lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            overlay
          ]);

          # A service's venv = that member's dependency closure from the
          # shared lock. workspace.deps.default maps every workspace
          # package to its enabled groups/extras ([] = plain deps);
          # selecting the service's entry yields exactly its closure.
          # Service members are virtual (no build-system) so nothing of
          # the app builds here — app code ships via copyToRoot, exactly
          # like the single-service template. restor8-core is editable →
          # built for real (hatchling) and installed into every venv.
          venvFor = name:
            pythonSet.mkVirtualEnv "restor8-${name}-env" {
              "restor8-${name}" = workspace.deps.default."restor8-${name}" or [ ];
            };
        in
        { inherit pkgs pythonSet venvFor; };

      # One OCI image per service. No Dockerfile: the uv2nix venv + app/
      # + a non-root user are copied in by dockerTools.buildImage. Cmd
      # strings are NOT auto-scanned into the closure, hence copyToRoot.
      mkServiceImage = system: name: appDir:
        let
          ps = perSystem system;
          pkgs = ps.pkgs;
          venv = ps.venvFor name;

          # Wrap app/ in a runCommand: a bare path in copyToRoot flattens
          # its contents to the image root (→ /main.py), we need /app/.
          appSource = pkgs.runCommand "restor8-${name}-app" { } ''
            mkdir -p $out/app
            cp -r ${appDir}/. $out/app/
          '';

          # /etc/passwd + /etc/group for UID 1000 so the manifest can run
          # runAsNonRoot (matches config.User = "1000:1000" below).
          nonRootUser = pkgs.runCommand "restor8-non-root-user" { } ''
            mkdir -p $out/etc
            printf 'root:x:0:0::/root:/bin/sh\nappuser:x:1000:1000::/app:/bin/sh\n' > $out/etc/passwd
            printf 'root:x:0:\nappuser:x:1000:\n'                                 > $out/etc/group
          '';

          # The one per-service image-content deviation (spec §2 anticipated
          # stateful backup): GitPython drives the git CLI, so the backup
          # image carries the git binary.
          extraRoot = pkgs.lib.optionals (name == "backup") [ pkgs.git ];
        in
        pkgs.dockerTools.buildImage {
          name = "localhost:5000/restor8-${name}";
          tag = "latest";
          copyToRoot = [ venv appSource nonRootUser ] ++ extraRoot;
          config = {
            User = "1000:1000";
            WorkingDir = "/app";
            Cmd = [ "${venv}/bin/uvicorn" "main:app" "--host" "0.0.0.0" "--port" "8080" ];
            ExposedPorts = { "8080/tcp" = { }; };
          };
        };

      # Dev shell factory: a service's venv + the pipeline toolbelt. The
      # venv is the SAME one the image ships — local `just run` and the
      # pod run identical interpreters and deps.
      mkDevShell = system: name:
        let
          ps = perSystem system;
          pkgs = ps.pkgs;
        in
        pkgs.mkShell {
          packages = [
            (ps.venvFor name)
            pkgs.uv             # `uv lock` after editing any pyproject
            pkgs.ruff pkgs.mypy
            pkgs.just pkgs.skopeo pkgs.kubectl
            pkgs.websocat       # raw WS client for the Phase 6 checkpoint
          ];
          shellHook = ''
            echo ""
            echo "  ❯ restor8 devshell  [service: ${name}]"
            echo "      venv    $(readlink -f ${ps.venvFor name})"
            echo "      image   localhost:5000/restor8-${name}:latest"
            echo "      run     just run ${name}    · deploy  just deploy ${name}"
            echo "      deps    edit pyproject.toml → \`uv lock\` → rebuild"
            echo ""
            # Undo nixpkgs PYTHONPATH propagation so the venv's site-packages win.
            unset PYTHONPATH
          '';
        };
    in
    {
      # One image per registry entry; `.#<name>` is the build target the
      # justfile uses (out-link result-<name>).
      packages = forAllSystems (system:
        let
          images = nixpkgs.lib.mapAttrs
            (name: appDir: mkServiceImage system name appDir)
            serviceApps;
        in
        images // { default = images.connector; });

      # Auto-loaded by direnv (.envrc → use flake). Default shell targets
      # the service under active development; named shells:
      #   nix develop .#connector
      devShells = forAllSystems (system:
        {
          default = mkDevShell system "connector";
        } // nixpkgs.lib.mapAttrs (name: _: mkDevShell system name) serviceApps);
    };
}
