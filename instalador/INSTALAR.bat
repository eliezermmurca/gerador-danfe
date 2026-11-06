@echo off
chcp 65001 >nul
title Instalador - Gerador de DANFE
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar.ps1" %*
