const chain = () => {
  const schema = { default: () => schema }
  return schema
}

export default {
  string: () => chain(),
  number: () => chain(),
  boolean: () => chain(),
  object: () => chain(),
  // Real schemastery validates against a closed set; the stub only has to keep
  // the shape of `Config` intact, but it still rejects an empty union so a
  // mistake in the plugin is visible.
  union: (values) => {
    if (!Array.isArray(values) || values.length === 0) {
      throw new Error('z.union requires at least one value')
    }
    return chain()
  },
}
