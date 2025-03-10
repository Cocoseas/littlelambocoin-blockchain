#!/bin/bash

if [ ! "$1" ]; then
  echo "This script requires either amd64 of arm64 as an argument"
	exit 1
elif [ "$1" = "amd64" ]; then
	PLATFORM="$1"
	DIR_NAME="littlelambocoin-blockchain-linux-x64"
else
	PLATFORM="$1"
	DIR_NAME="littlelambocoin-blockchain-linux-arm64"
fi

pip install setuptools_scm
# The environment variable LITTLELAMBOCOIN_INSTALLER_VERSION needs to be defined
# If the env variable NOTARIZE and the username and password variables are
# set, this will attempt to Notarize the signed DMG
LITTLELAMBOCOIN_INSTALLER_VERSION=$(python installer-version.py)

if [ ! "$LITTLELAMBOCOIN_INSTALLER_VERSION" ]; then
	echo "WARNING: No environment variable LITTLELAMBOCOIN_INSTALLER_VERSION set. Using 0.0.0."
	LITTLELAMBOCOIN_INSTALLER_VERSION="0.0.0"
fi
echo "Littlelambocoin Installer Version is: $LITTLELAMBOCOIN_INSTALLER_VERSION"

echo "Installing npm and electron packagers"
cd npm_linux_deb || exit
npm install
PATH=$(npm bin):$PATH
cd .. || exit

echo "Create dist/"
rm -rf dist
mkdir dist

echo "Create executables with pyinstaller"
pip install pyinstaller==4.9
SPEC_FILE=$(python -c 'import littlelambocoin; print(littlelambocoin.PYINSTALLER_SPEC_PATH)')
pyinstaller --log-level=INFO "$SPEC_FILE"
LAST_EXIT_CODE=$?
if [ "$LAST_EXIT_CODE" -ne 0 ]; then
	echo >&2 "pyinstaller failed!"
	exit $LAST_EXIT_CODE
fi

cp -r dist/daemon ../littlelambocoin-blockchain-gui/packages/gui
cd .. || exit
cd littlelambocoin-blockchain-gui || exit

echo "npm build"
lerna clean -y
npm install
# Audit fix does not currently work with Lerna. See https://github.com/lerna/lerna/issues/1663
# npm audit fix
npm run build
LAST_EXIT_CODE=$?
if [ "$LAST_EXIT_CODE" -ne 0 ]; then
	echo >&2 "npm run build failed!"
	exit $LAST_EXIT_CODE
fi

# Change to the gui package
cd packages/gui || exit

# sets the version for littlelambocoin-blockchain in package.json
cp package.json package.json.orig
jq --arg VER "$LITTLELAMBOCOIN_INSTALLER_VERSION" '.version=$VER' package.json > temp.json && mv temp.json package.json

electron-packager . littlelambocoin-blockchain --asar.unpack="**/daemon/**" --platform=linux \
--icon=src/assets/img/Littlelambocoin.icns --overwrite --app-bundle-id=net.littlelambocoin.blockchain \
--appVersion=$LITTLELAMBOCOIN_INSTALLER_VERSION --executable-name=littlelambocoin-blockchain
LAST_EXIT_CODE=$?

# reset the package.json to the original
mv package.json.orig package.json

if [ "$LAST_EXIT_CODE" -ne 0 ]; then
	echo >&2 "electron-packager failed!"
	exit $LAST_EXIT_CODE
fi

mv $DIR_NAME ../../../build_scripts/dist/
cd ../../../build_scripts || exit

echo "Create littlelambocoin-$LITTLELAMBOCOIN_INSTALLER_VERSION.deb"
rm -rf final_installer
mkdir final_installer
electron-installer-debian --src dist/$DIR_NAME/ --dest final_installer/ \
--arch "$PLATFORM" --options.version $LITTLELAMBOCOIN_INSTALLER_VERSION --options.bin littlelambocoin-blockchain --options.name littlelambocoin-blockchain
LAST_EXIT_CODE=$?
if [ "$LAST_EXIT_CODE" -ne 0 ]; then
	echo >&2 "electron-installer-debian failed!"
	exit $LAST_EXIT_CODE
fi

ls final_installer/
