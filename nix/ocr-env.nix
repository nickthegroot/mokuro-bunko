{
  python3,
}:
# src/mokuro_bunko/ocr/installer.py
python3.withPackages (
  python-pkgs: with python-pkgs; [
    torch
    torchvision
    (toPythonModule pkgs.mokuro)
  ]
)
