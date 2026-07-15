module.exports = {
  testEnvironment: 'node',
  roots: ['<rootDir>/test'],
  testMatch: ['**/*.test.ts'],
  transform: {
    '^.+\\.ts$': [
      'ts-jest',
      {
        tsconfig: {
          declaration: false,
          inlineSourceMap: false,
          sourceMap: true,
        },
      },
    ],
  },
};
