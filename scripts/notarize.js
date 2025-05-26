console.log(`[DEBUG] dotenv 로드 전 process.env.APPLE_TEAM_ID: ${process.env.APPLE_TEAM_ID}`);
require('dotenv').config(); // .env 파일에서 환경변수를 로드합니다.
const { notarize } = require('@electron/notarize');

exports.default = async function notarizing(context) {
  const { electronPlatformName, appOutDir } = context;

  // macOS 빌드가 아니면 notarization을 건너뜁니다.
  if (electronPlatformName !== 'darwin') {
    console.log('Not a macOS build, skipping notarization.');
    return;
  }

  console.log('Starting notarization for macOS build.');

  const appName = context.packager.appInfo.productFilename;
  const appPath = `${appOutDir}/${appName}.app`;
  const appBundleId = context.packager.config.appId || context.packager.appInfo.macBundleIdentifier; // package.json의 appId 사용

  // 환경 변수에서 Apple ID, 앱 특정 암호, Team ID를 가져옵니다.
  // 이전에 .zshrc 또는 .env 파일에 설정하신 값을 사용합니다.
  const appleId = process.env.APPLE_ID;
  const appleIdPassword = process.env.APPLE_APP_SPECIFIC_PASSWORD;
  const appleTeamId = process.env.APPLE_TEAM_ID; // package.json의 identity에서 추출된 Team ID와 일치해야 합니다.

  if (!appleId) {
    console.error('APPLE_ID environment variable is not set. Skipping notarization.');
    // 빌드 실패를 원치 않으면 이 부분에서 throw new Error 대신 return; 처리 가능
    throw new Error('APPLE_ID environment variable is not set.');
  }
  if (!appleIdPassword) {
    console.error('APPLE_APP_SPECIFIC_PASSWORD environment variable is not set. Skipping notarization.');
    throw new Error('APPLE_APP_SPECIFIC_PASSWORD environment variable is not set.');
  }
  if (!appleTeamId) {
    console.warn('APPLE_TEAM_ID environment variable is not set. Attempting notarization without it, but it might be required if your Apple ID is associated with multiple teams.');
    // Apple ID가 여러 팀에 속해있지 않다면 teamId 없이도 성공할 수 있습니다.
    // 하지만 명시하는 것이 좋습니다.
  }
  if (!appBundleId) {
    console.error('Could not determine appBundleId. Skipping notarization.');
    throw new Error('Could not determine appBundleId.');
  }

  console.log(`Notarizing ${appBundleId} found at ${appPath} with Apple ID: ${appleId} and Team ID: ${appleTeamId || 'not specified'}`);

  try {
    await notarize({
      appBundleId: appBundleId,
      appPath: appPath,
      appleId: appleId,
      appleIdPassword: appleIdPassword,
      teamId: appleTeamId, // teamId를 명시적으로 제공
    });
    console.log('Notarization successful!');
  } catch (error) {
    console.error('Notarization failed:');
    console.error(error);
    throw error; // 빌드 과정을 중단시키려면 에러를 다시 throw 합니다.
  }
};