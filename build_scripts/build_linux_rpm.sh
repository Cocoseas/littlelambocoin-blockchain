#!/bin/bash

if [ ! "$1" ]; then
  echo "This script requires either amd64 of arm64 as an argument"
	exit 1
elif [ "$1" = "amd64" ]; then
	#PLATFORM="$1"
	REDHAT_PLATFORM="x86_64"
	DIR_NAME="littlelambocoin-blockchain-linux-x64"
else
	#PLATFORM="$1"
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
cd npm_linux_rpm || exit
npm install
GLOBAL_NPM_ROOT=$(pwd)/node_modules
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

if [ "$REDHAT_PLATFORM" = "x86_64" ]; then
	echo "Create littlelambocoin-blockchain-$LITTLELAMBOCOIN_INSTALLER_VERSION.rpm"

	# Disables build links from the generated rpm so that we dont conflict with other packages. See https://github.com/BTCgreen-Network/littlelambocoin-blockchain/issues/3846
	# shellcheck disable=SC2086
	sed -i '1s/^/%define _build_id_links none\n%global _enable_debug_package 0\n%global debug_package %{nil}\n%global __os_install_post \/usr\/lib\/rpm\/brp-compress %{nil}\n/' "$GLOBAL_NPM_ROOT/electron-installer-redhat/resources/spec.ejs"

	# Use attr feature of RPM to set the chrome-sandbox permissions
	# adds a %attr line after the %files line
	# The location is based on the existing location inside spec.ej
	sed -i '/^%files/a %attr(4755, root, root) /usr/lib/<%= name %>/chrome-sandbox' "$GLOBAL_NPM_ROOT/electron-installer-redhat/resources/spec.ejs"

	# Updates the requirements for building an RPM on Centos 7 to allow older version of rpm-build and not use the boolean dependencies
	# See https://github.com/electron-userland/electron-installer-redhat/issues/157
	# shellcheck disable=SC2086
	sed -i "s#throw new Error('Please upgrade to RPM 4.13.*#console.warn('You are using RPM < 4.13')\n      return { requires: [ 'gtk3', 'libnotify', 'nss', 'libXScrnSaver', 'libXtst', 'xdg-utils', 'at-spi2-core', 'libdrm', 'mesa-libgbm', 'libxcb' ] }#g" $GLOBAL_NPM_ROOT/electron-installer-redhat/src/dependencies.js

  electron-installer-redhat --src dist/$DIR_NAME/ --dest final_installer/ \
  --arch "$REDHAT_PLATFORM" --options.version $LITTLELAMBOCOIN_INSTALLER_VERSION \
  --license ../LICENSE --options.bin littlelambocoin-blockchain --options.name littlelambocoin-blockchain
  LAST_EXIT_CODE=$?
  if [ "$LAST_EXIT_CODE" -ne 0 ]; then
	  echo >&2 "electron-installer-redhat failed!"
	  exit $LAST_EXIT_CODE
  fi
fi

ls final_installer/
