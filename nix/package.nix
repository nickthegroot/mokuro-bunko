{
  lib,
  python3Packages,
}:

python3Packages.buildPythonApplication {
  pname = "mokuro-bunko";
  version = "0.1.3";
  pyproject = true;

  src = lib.cleanSource ../.;

  build-system = with python3Packages; [ hatchling ];

  dependencies = with python3Packages; [
    wsgidav
    cheroot
    pyyaml
    bcrypt
    watchdog
    pillow
    click
    cryptography
  ];

  meta = {
    description = "Self-hosted manga library server with WebDAV, OCR, and multi-user support";
    homepage = "https://github.com/Gnathonic/mokuro-bunko";
    license = lib.licenses.mpl20;
    mainProgram = "mokuro-bunko";
    maintainers = [ ];
  };
}
