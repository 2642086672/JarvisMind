const chain = () => {
  const schema = { default: () => schema }
  return schema
}

export default {
  string: () => chain(),
  number: () => chain(),
  boolean: () => chain(),
  object: () => chain(),
}