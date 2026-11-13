@echo off

where python
python --version

python -c "import git; print(git.__version__)"

python main.py

pause